"""Discovery providers. Each returns a list of RawLead. All are optional and keyed by env vars.

- Google results: Serper.dev (SERPER_API_KEY) or Brave Search API (BRAVE_API_KEY).
  (Google's own Custom Search JSON API is closed to new customers.)
- X posts: official X API v2 recent search (X_BEARER_TOKEN) or TwitterAPI.io (TWITTERAPI_IO_KEY).
- Telegram: public channels the owner lists, read through the owner's connected account.
- Company boards: the existing job feed (already in the jobs table).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from ..classify import html_to_text
from ..config import get_settings
from . import llm

log = logging.getLogger("search")
UA = {"User-Agent": "Mozilla/5.0 (compatible; Web3JobFinder/1.0; +https://web3-job-finder.onrender.com)"}


class ProviderError(Exception):
    pass


@dataclass
class RawLead:
    source: str              # google | x | telegram | board
    url: str
    title: str = ""
    text: str = ""
    project: str = ""
    posted_at: datetime | None = None
    author_x: str = ""
    author_tg: str = ""
    author_name: str = ""
    extra_contacts: list[dict] = field(default_factory=list)
    checked: bool = False    # True when we actually loaded the source content


def _naive(dt: datetime | None) -> datetime | None:
    if dt and dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%a %b %d %H:%M:%S %z %Y"):
        try:
            return _naive(datetime.strptime(str(v), fmt))
        except ValueError:
            pass
    try:
        return _naive(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


def configured() -> dict:
    st = get_settings()
    return {
        "google": "serper" if st.serper_api_key else "brave" if st.brave_api_key else "",
        "x": "chatgpt" if st.openai_api_key else "grok" if st.xai_api_key else "x_api" if st.x_bearer_token else "twitterapi_io" if st.twitterapi_io_key else "",
        "llm": llm.enabled("qualify") or llm.enabled("draft"),
        "llm_draft": llm.provider_name("draft"),
        "llm_qualify": llm.provider_name("qualify"),
    }


# ------------------------------------------------------------------ Google-style web search
async def web_search(query: str, num: int = 10) -> list[RawLead]:
    st = get_settings()
    async with httpx.AsyncClient(timeout=20) as c:
        if st.serper_api_key:
            r = await c.post("https://google.serper.dev/search",
                             json={"q": query, "num": num, "tbs": "qdr:m"},
                             headers={"X-API-KEY": st.serper_api_key})
            if r.status_code != 200:
                raise ProviderError(f"Serper HTTP {r.status_code}")
            items = r.json().get("organic", [])
            return [RawLead("google", i.get("link", ""), i.get("title", ""), i.get("snippet", ""),
                            posted_at=_parse_dt(i.get("date"))) for i in items if i.get("link")]
        if st.brave_api_key:
            r = await c.get("https://api.search.brave.com/res/v1/web/search",
                            params={"q": query, "count": num, "freshness": "pm"},
                            headers={"X-Subscription-Token": st.brave_api_key, "Accept": "application/json"})
            if r.status_code != 200:
                raise ProviderError(f"Brave HTTP {r.status_code}")
            items = (r.json().get("web") or {}).get("results", [])
            return [RawLead("google", i.get("url", ""), i.get("title", ""), html_to_text(i.get("description", "")),
                            posted_at=_parse_dt(i.get("page_age"))) for i in items if i.get("url")]
    raise ProviderError("No search API key configured (SERPER_API_KEY or BRAVE_API_KEY)")


SKIP_DOMAINS = ("linkedin.com/login", "facebook.com", "instagram.com", "accounts.google")


async def enrich_page(lead: RawLead, max_bytes: int = 600_000) -> RawLead:
    """Loads the public page so we can read the real text and contacts. Marks `checked`."""
    if not lead.url.startswith("http") or any(d in lead.url for d in SKIP_DOMAINS):
        return lead
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=UA) as c:
            r = await c.get(lead.url)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "") and len(r.content) <= max_bytes:
            body = r.text
            if not lead.title:
                m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
                lead.title = html_to_text(m.group(1)).strip()[:200] if m else ""
            # keep raw t.me / mailto / x links which html_to_text would drop
            links = " ".join(re.findall(r'href="((?:https?://(?:t\.me|telegram\.me|x\.com|twitter\.com)/[^"]+)|mailto:[^"]+)"', body))
            text = html_to_text(re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body))
            lead.text = (lead.text + "\n\n" + text[:6000] + "\n" + links.replace("mailto:", "")).strip()
            lead.checked = True
    except Exception as e:
        log.info("page fetch failed %s: %s", lead.url[:80], type(e).__name__)
    return lead


# ------------------------------------------------------------------ X
X_ROLE_QUERY = ('(hiring OR "we are hiring" OR "looking for" OR "we\'re looking") '
                '(moderator OR "community manager" OR "customer support" OR "social media manager" '
                'OR "business development" OR ambassador OR "community lead") '
                '(web3 OR crypto OR defi OR nft OR blockchain) -is:retweet lang:en')


async def x_search(query: str = "", max_results: int = 20) -> list[RawLead]:
    st = get_settings()
    q = query or X_ROLE_QUERY
    if st.openai_api_key:
        return await grok_x_search(query, max_results, provider="chatgpt")
    if st.xai_api_key:
        return await grok_x_search(query, max_results)
    async with httpx.AsyncClient(timeout=25) as c:
        if st.x_bearer_token:
            r = await c.get("https://api.x.com/2/tweets/search/recent",
                            params={"query": q, "max_results": max(10, min(100, max_results)),
                                    "tweet.fields": "created_at,author_id,entities",
                                    "expansions": "author_id",
                                    "user.fields": "username,name,description,url"},
                            headers={"Authorization": f"Bearer {st.x_bearer_token}"})
            if r.status_code != 200:
                raise ProviderError(f"X API HTTP {r.status_code}")
            data = r.json()
            users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
            out = []
            for t in data.get("data", []):
                u = users.get(t.get("author_id"), {})
                uname = u.get("username", "")
                text = t.get("text", "") + ("\n\nAuthor bio: " + u["description"] if u.get("description") else "")
                out.append(RawLead("x", f"https://x.com/{uname or 'i'}/status/{t['id']}", "", text,
                                   project=u.get("name", ""), posted_at=_parse_dt(t.get("created_at")),
                                   author_x=uname, author_name=u.get("name", ""), checked=True))
            return out
        if st.twitterapi_io_key:
            r = await c.get("https://api.twitterapi.io/twitter/tweet/advanced_search",
                            params={"query": q.replace("-is:retweet", "-filter:retweets"), "queryType": "Latest"},
                            headers={"X-API-Key": st.twitterapi_io_key})
            if r.status_code != 200:
                raise ProviderError(f"TwitterAPI.io HTTP {r.status_code}")
            out = []
            for t in (r.json().get("tweets") or [])[:max_results]:
                a = t.get("author") or {}
                uname = a.get("userName", "")
                text = t.get("text", "") + ("\n\nAuthor bio: " + a["description"] if a.get("description") else "")
                out.append(RawLead("x", t.get("url") or f"https://x.com/{uname}/status/{t.get('id')}", "", text,
                                   project=a.get("name", ""), posted_at=_parse_dt(t.get("createdAt")),
                                   author_x=uname, author_name=a.get("name", ""), checked=True))
            return out
    raise ProviderError("No X key configured (XAI_API_KEY, X_BEARER_TOKEN or TWITTERAPI_IO_KEY)")


GROK_X_PROMPT = """Use X search to find recent X posts (last {days} days) where a Web3 / crypto project or a person
is HIRING for one of: moderator, community manager, customer support, social media manager, business development /
growth, community operations, ambassador or contributor roles. {extra}
Only include posts you actually fetched with the search tool. Never invent posts, handles or links.
Return ONLY JSON: {{"posts": [{{"url": "https://x.com/<handle>/status/<id>", "author": "<handle without @>",
"name": "<display name>", "text": "<the post text, verbatim>", "date": "YYYY-MM-DD", "author_bio": "<bio if seen>"}}]}}
Up to {n} posts, newest first."""

_STATUS_RE = re.compile(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")


def _grok_citations(data: dict) -> set[str]:
    """Every URL Grok's tools actually returned (top-level citations + url_citation annotations)."""
    urls = set(data.get("citations") or [])
    for item in data.get("output") or []:
        for part in item.get("content") or []:
            for a in part.get("annotations") or []:
                if a.get("url"):
                    urls.add(a["url"])
    ids = set()
    for u in urls:
        m = _STATUS_RE.search(u if isinstance(u, str) else str(u))
        if m:
            ids.add(m.group(2))
    return ids


def _grok_text(data: dict) -> str:
    if data.get("output_text"):
        return data["output_text"]
    out = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    out.append(part.get("text", ""))
    return "\n".join(out)


async def grok_x_search(query: str = "", max_results: int = 20, days: int = 14,
                        provider: str = "grok") -> list[RawLead]:
    """X search through Grok's x_search tool, or ChatGPT's web_search limited to x.com.
    Keeps only posts the search tool really cited."""
    st = get_settings()
    today = datetime.utcnow().date()
    prompt = GROK_X_PROMPT.format(days=days, n=max_results,
                                  extra=f"Focus on: {query}" if query and query != X_ROLE_QUERY else "")
    body = {
        "model": st.xai_model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "x_search",
                   "from_date": (today - timedelta(days=days)).isoformat(),
                   "to_date": (today + timedelta(days=1)).isoformat()}],
        "max_turns": 3,
    }
    if provider == "chatgpt":
        body = {"model": st.openai_model,
                "input": [{"role": "user", "content": prompt.replace("Use X search", "Use web search on x.com")}],
                "tools": [{"type": "web_search", "filters": {"allowed_domains": ["x.com", "twitter.com"]}}]}
        url, key = "https://api.openai.com/v1/responses", st.openai_api_key
    else:
        url, key = "https://api.x.ai/v1/responses", st.xai_api_key
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise ProviderError(f"{provider} HTTP {r.status_code}")
    data = r.json()
    cited = _grok_citations(data)
    m = re.search(r"\{.*\}", _grok_text(data), re.S)
    try:
        posts = (json.loads(m.group(0)) if m else {}).get("posts") or []
    except json.JSONDecodeError:
        posts = []
    out, seen = [], set()
    for p in posts[:max_results]:
        mm = _STATUS_RE.search(str(p.get("url", "")))
        if not mm or mm.group(2) in seen:
            continue
        sid = mm.group(2)
        seen.add(sid)
        if cited and sid not in cited:
            continue   # Grok named a post its search never returned: drop it
        uname = (p.get("author") or mm.group(1)).lstrip("@")
        text = str(p.get("text", ""))[:4000]
        if p.get("author_bio"):
            text += "\n\nAuthor bio: " + str(p["author_bio"])[:500]
        out.append(RawLead("x", f"https://x.com/{uname}/status/{sid}", "", text,
                           project=str(p.get("name", "")), posted_at=_parse_dt(p.get("date")),
                           author_x=uname, author_name=str(p.get("name", "")),
                           checked=sid in cited))   # only "checked" when Grok's search tool cited it
    return out


# ------------------------------------------------------------------ Telegram public channels
async def telegram_channels(owner_id: int, channels: list[str], per_channel: int = 40) -> list[RawLead]:
    """Reads recent posts from PUBLIC channels/groups the owner listed, via their own session."""
    from .. import userclient as uc
    from . import tg
    if not channels:
        return []
    client = await tg.open_client(owner_id)
    out: list[RawLead] = []
    try:
        for ch in channels[:15]:
            name = ch.strip().lstrip("@").replace("https://t.me/", "")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
                continue
            try:
                ent = await client.get_entity(name)
                if not getattr(ent, "username", None):
                    continue  # only public chats
                async for msg in client.iter_messages(ent, limit=per_channel):
                    text = getattr(msg, "message", "") or ""
                    if len(text) < 40:
                        continue
                    author_tg = ""
                    sender = getattr(msg, "sender", None)
                    if sender is not None and getattr(sender, "username", None) and not getattr(sender, "bot", False) \
                            and getattr(sender, "id", None) != getattr(ent, "id", None):
                        author_tg = sender.username
                    out.append(RawLead("telegram", f"https://t.me/{ent.username}/{msg.id}", "", text,
                                       project="", posted_at=_naive(msg.date), author_tg=author_tg,
                                       checked=True))
            except Exception as e:
                log.info("channel %s skipped: %s", name, type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    break
    finally:
        await client.disconnect()
    return out
