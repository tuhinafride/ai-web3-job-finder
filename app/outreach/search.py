"""Discovery providers. Each returns a list of RawLead. All are optional and keyed by env vars.

- Google results, in order: Serper.dev (SERPER_API_KEY) > Brave Search API (BRAVE_API_KEY) >
  Gemini "Grounding with Google Search" (free with a Gemini key). Google's own Custom Search JSON API is
  closed to new customers. Gemini results are kept ONLY when they are grounding sources Google returned
  (groundingMetadata.groundingChunks); the redirect links are resolved to the real page and the page is read.
- X posts, in order: official X API v2 (X_BEARER_TOKEN) > TwitterAPI.io (TWITTERAPI_IO_KEY) >
  Gemini grounding limited to x.com hiring posts (only grounded x.com/twitter.com status links are kept) >
  ChatGPT web search on x.com (OPENAI_API_KEY, optional).
- Every provider has an exponential back-off after failures, so a broken key is skipped, never faked.
- Telegram: public channels the owner lists, read through the owner's connected account.
- Company boards: the existing job feed (already in the jobs table).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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


class Backoff:
    """Per-provider exponential back-off: 1, 2, 4 … 60 minutes after consecutive failures."""
    BASE, CAP = 60.0, 3600.0

    def __init__(self):
        self.state: dict[str, dict] = {}

    def allowed(self, name: str) -> bool:
        st = self.state.get(name)
        return not st or st["until"] <= time.monotonic()

    def fail(self, name: str, err: str):
        st = self.state.setdefault(name, {"fails": 0, "until": 0.0, "error": ""})
        st["fails"] += 1
        st["until"] = time.monotonic() + min(self.CAP, self.BASE * 2 ** (st["fails"] - 1))
        st["error"] = err[:300]

    def ok(self, name: str):
        self.state.pop(name, None)

    def wait_left(self, name: str) -> int:
        st = self.state.get(name)
        return max(0, int(st["until"] - time.monotonic())) if st else 0

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {k: {"fails": v["fails"], "retry_in_sec": max(0, int(v["until"] - now)), "error": v["error"]}
                for k, v in self.state.items()}

    def clear(self):
        self.state.clear()


backoff = Backoff()


async def _guarded(name: str, coro_fn, errors: list[str] | None = None):
    """Runs one provider call with back-off bookkeeping. Returns None when skipped/failed."""
    if not backoff.allowed(name):
        msg = f"{name}: skipped (backing off {backoff.wait_left(name)}s after errors)"
        if errors is not None:
            errors.append(msg)
        return None
    try:
        res = await coro_fn()
    except ProviderError as e:
        backoff.fail(name, str(e))
        if errors is not None:
            errors.append(str(e))
        log.info("provider %s failed: %s", name, e)
        return None
    except Exception as e:  # network errors, bad JSON … never crash a run
        backoff.fail(name, type(e).__name__)
        if errors is not None:
            errors.append(f"{name}: {type(e).__name__}")
        log.info("provider %s crashed: %s", name, type(e).__name__)
        return None
    backoff.ok(name)
    return res


def _gemini_ready() -> bool:
    st = get_settings()
    return bool(st.gemini_api_key)


def google_providers() -> list[str]:
    st = get_settings()
    out = []
    if st.serper_api_key:
        out.append("serper")
    if st.brave_api_key:
        out.append("brave")
    if _gemini_ready():
        out.append("gemini")
    return out


def x_providers() -> list[str]:
    st = get_settings()
    out = []
    if st.x_bearer_token:
        out.append("x_api")
    if st.twitterapi_io_key:
        out.append("twitterapi_io")
    if _gemini_ready():
        out.append("gemini_x")
    if st.openai_api_key:
        out.append("chatgpt_x")
    return out


def configured() -> dict:
    g, x = google_providers(), x_providers()
    return {
        "google": g[0] if g else "", "google_chain": g,
        "x": x[0] if x else "", "x_chain": x,
        "llm": llm.enabled("qualify") or llm.enabled("draft"),
        "llm_draft": llm.provider_name("draft"),
        "llm_qualify": llm.provider_name("qualify"),
    }


# ------------------------------------------------------------------ Google-style web search
async def _serper(query: str, num: int) -> list[RawLead]:
    st = get_settings()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://google.serper.dev/search", json={"q": query, "num": num, "tbs": "qdr:m"},
                         headers={"X-API-KEY": st.serper_api_key})
    if r.status_code != 200:
        raise ProviderError(f"Serper HTTP {r.status_code}")
    items = r.json().get("organic", [])
    return [RawLead("google", i.get("link", ""), i.get("title", ""), i.get("snippet", ""),
                    posted_at=_parse_dt(i.get("date"))) for i in items if i.get("link")]


async def _brave(query: str, num: int) -> list[RawLead]:
    st = get_settings()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": num, "freshness": "pm"},
                        headers={"X-Subscription-Token": st.brave_api_key, "Accept": "application/json"})
    if r.status_code != 200:
        raise ProviderError(f"Brave HTTP {r.status_code}")
    items = (r.json().get("web") or {}).get("results", [])
    return [RawLead("google", i.get("url", ""), i.get("title", ""), html_to_text(i.get("description", "")),
                    posted_at=_parse_dt(i.get("page_age"))) for i in items if i.get("url")]


async def web_search(query: str, num: int = 10, errors: list[str] | None = None) -> list[RawLead]:
    """One query through the paid search APIs: Serper > Brave (first that works)."""
    errs: list[str] = [] if errors is None else errors
    st = get_settings()
    chain = [n for n in ("serper", "brave") if (st.serper_api_key if n == "serper" else st.brave_api_key)]
    if not chain:
        raise ProviderError("No Google search API key (SERPER_API_KEY or BRAVE_API_KEY)")
    for name in chain:
        fn = (lambda: _serper(query, num)) if name == "serper" else (lambda: _brave(query, num))
        res = await _guarded(name, fn, errs)
        if res is not None:
            return res
    raise ProviderError("; ".join(errs[-len(chain):]) or "Google search providers failed")


async def google_search(queries: list[str]) -> tuple[list[RawLead], list[str]]:
    """Serper > Brave per query; if neither gave results, ONE Gemini grounding call for all queries.
    Returns (leads, errors). Failures are reported, never replaced by invented results."""
    errs: list[str] = []
    out: list[RawLead] = []
    st = get_settings()
    paid = bool(st.serper_api_key or st.brave_api_key)
    if paid or not _gemini_ready():
        for q in queries:
            try:
                out += await web_search(q)
            except ProviderError as e:
                errs.append(str(e))
                break
    if not out and _gemini_ready():
        res = await _guarded("gemini", lambda: gemini_grounded_search(queries), errs)
        if res is not None:
            out += res
    seen, uniq = set(), []
    for r in out:
        if r.url and r.url not in seen:
            seen.add(r.url)
            uniq.append(r)
    return uniq, errs


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


# ------------------------------------------------------------------ Gemini grounding with Google Search
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_grounding_daily = {"day": "", "count": 0}

GROUND_WEB_PROMPT = """Search Google for job postings published in the last {days} days where a Web3 / crypto /
blockchain project is HIRING for one of these roles: community manager, community moderator (Discord / Telegram),
customer support, social media manager, business development / growth, community operations, ambassador or paid
contributor. Prefer the original posting (company careers page, job board listing, official announcement).
Search topics: {queries}
Reply with ONLY a JSON object, no prose:
{{"jobs": [{{"title": "...", "project": "...", "url": "<the page where you saw it>", "snippet": "<one or two
sentences copied from the page>", "date": "YYYY-MM-DD or empty", "pay": "<only if stated>"}}]}}
Include only postings you actually found in the search results. Never invent jobs, links or details.
Up to {n} jobs."""

GROUND_X_PROMPT = """Search Google for recent posts on X (Twitter) from the last {days} days, using queries like
site:x.com hiring web3 moderator, site:x.com "we are hiring" crypto "community manager", site:x.com hiring
"customer support" crypto, site:x.com hiring ambassador web3. Only posts where a Web3 / crypto project or person is
hiring for: moderator, community manager, customer support, social media manager, business development / growth,
community operations, ambassador or contributor roles. {extra}
Reply with ONLY a JSON object, no prose:
{{"posts": [{{"url": "https://x.com/<handle>/status/<id>", "author": "<handle without @>", "name": "<display
name>", "text": "<the post text as shown in the search result>", "date": "YYYY-MM-DD or empty"}}]}}
Include only posts that appeared in your search results. Never invent posts, handles or links. Up to {n} posts."""


def grounding_left() -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _grounding_daily["day"] != day:
        _grounding_daily.update(day=day, count=0)
    return max(0, get_settings().gemini_grounding_daily_cap - _grounding_daily["count"])


async def gemini_generate_grounded(prompt: str, timeout: float = 90) -> dict:
    """POST models/{model}:generateContent with the google_search tool. Returns the raw response JSON."""
    st = get_settings()
    key = st.gemini_api_key
    if not key:
        raise ProviderError("Gemini grounding needs LLM_API_KEY (Gemini) or GEMINI_API_KEY")
    if grounding_left() <= 0:
        raise ProviderError("Gemini grounding: daily cap reached (GEMINI_GROUNDING_DAILY_CAP)")
    model = st.gemini_search_model
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2}}
    if re.search(r"2\.5-flash", model):
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}   # saves free-tier tokens
    tried_fallback = False
    async with httpx.AsyncClient(timeout=timeout) as c:
        for attempt in range(4):
            _grounding_daily["count"] += 1
            await llm.throttle()
            r = await c.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                             headers={"x-goog-api-key": key})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and attempt < 2 and not llm._is_daily_quota(r.text):
                await asyncio.sleep(llm.retry_delay(r, attempt))
                continue
            if r.status_code == 400 and "thinkingConfig" in body["generationConfig"] and "thinking" in r.text.lower():
                body["generationConfig"].pop("thinkingConfig")
                continue
            if llm._model_missing(r) and not tried_fallback and st.llm_fallback_model and model != st.llm_fallback_model:
                model, tried_fallback = st.llm_fallback_model, True
                body["generationConfig"].pop("thinkingConfig", None)
                continue
            hint = " (rate limit)" if r.status_code == 429 else \
                " (invalid API key)" if llm.bad_key(r) else ""
            raise ProviderError(f"Gemini grounding HTTP {r.status_code}{hint}")
    raise ProviderError("Gemini grounding: gave up after retries")


def grounding_chunks(data: dict) -> list[dict]:
    """[{'uri','title'}] from candidates[0].groundingMetadata.groundingChunks[].web — the ONLY trusted results."""
    try:
        cand = (data.get("candidates") or [])[0]
    except IndexError:
        return []
    meta = cand.get("groundingMetadata") or {}
    out, seen = [], set()
    for ch in meta.get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri = web.get("uri") or ""
        if uri.startswith("http") and uri not in seen:
            seen.add(uri)
            out.append({"uri": uri, "title": web.get("title") or ""})
    return out


def grounding_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))


def _json_items(text: str, key: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get(key) or []
    except (json.JSONDecodeError, AttributeError):
        return []
    return [i for i in items if isinstance(i, dict)]


async def resolve_redirect(uri: str, client: httpx.AsyncClient | None = None) -> str:
    """Gemini grounding URIs are vertexaisearch.cloud.google.com redirects. Returns the real URL or ''."""
    if "vertexaisearch.cloud.google.com" not in uri and "grounding-api-redirect" not in uri:
        return uri
    own = client is None
    c = client or httpx.AsyncClient(timeout=12, headers=UA)
    try:
        r = await c.get(uri, follow_redirects=False)
        loc = r.headers.get("location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc.startswith("http"):
            return loc
        r = await c.get(uri, follow_redirects=True)
        final = str(r.url)
        return "" if "vertexaisearch.cloud.google.com" in final else final
    except Exception as e:
        log.info("redirect resolve failed: %s", type(e).__name__)
        return ""
    finally:
        if own:
            await c.aclose()


def _norm(u: str) -> str:
    u = (u or "").lower().split("#")[0].split("?")[0].rstrip("/")
    return re.sub(r"^https?://(www\.|mobile\.)?", "", u).replace("twitter.com/", "x.com/")


async def _resolve_all(chunks: list[dict]) -> list[tuple[dict, str]]:
    async with httpx.AsyncClient(timeout=12, headers=UA) as c:
        real = await asyncio.gather(*(resolve_redirect(ch["uri"], c) for ch in chunks))
    return [(ch, u) for ch, u in zip(chunks, real) if u]


async def gemini_grounded_search(queries: list[str], max_results: int = 12, days: int = 30) -> list[RawLead]:
    """Google results via Gemini grounding. Only grounding chunks become leads; pages are then read."""
    prompt = GROUND_WEB_PROMPT.format(days=days, n=max_results, queries="; ".join(q for q in queries if q)[:600])
    data = await gemini_generate_grounded(prompt)
    chunks = grounding_chunks(data)[:max_results * 2]
    if not chunks:
        return []          # nothing grounded -> nothing kept (never use un-sourced model text)
    items = _json_items(grounding_text(data), "jobs")
    by_url = {_norm(str(i.get("url", ""))): i for i in items if i.get("url")}
    out: list[RawLead] = []
    seen = set()
    for ch, real in await _resolve_all(chunks):
        key = _norm(real)
        if key in seen:
            continue
        seen.add(key)
        it = by_url.get(key) or {}
        snippet = str(it.get("snippet") or "")[:600]
        lead = RawLead("google", real, str(it.get("title") or "")[:200],
                       (snippet + ("\nPay: " + str(it["pay"]) if it.get("pay") else "")).strip(),
                       project=str(it.get("project") or "")[:200], posted_at=_parse_dt(it.get("date")))
        out.append(lead)
        if len(out) >= max_results:
            break
    await asyncio.gather(*(enrich_page(l) for l in out))
    return out


async def x_oembed_text(url: str) -> str:
    """Real post text through X's public oEmbed endpoint (no key). '' when unavailable."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as c:
            r = await c.get("https://publish.twitter.com/oembed", params={"url": url, "omit_script": "1"})
        if r.status_code == 200:
            html = r.json().get("html", "")
            m = re.search(r"(?is)<p[^>]*>(.*?)</p>", html)
            return html_to_text(m.group(1)).strip() if m else ""
    except Exception:
        pass
    return ""


async def gemini_x_search(query: str = "", max_results: int = 15, days: int = 14) -> list[RawLead]:
    """X hiring posts via Gemini grounding (site:x.com). Keeps only grounded x.com/twitter.com status URLs."""
    extra = f"Focus on: {query}" if query and query != X_ROLE_QUERY else ""
    data = await gemini_generate_grounded(GROUND_X_PROMPT.format(days=days, n=max_results, extra=extra))
    chunks = grounding_chunks(data)
    if not chunks:
        return []
    posts = _json_items(grounding_text(data), "posts")
    by_id = {}
    for p in posts:
        m = _STATUS_RE.search(str(p.get("url", "")))
        if m:
            by_id[m.group(2)] = p
    out, seen = [], set()
    for ch, real in await _resolve_all(chunks):
        m = _STATUS_RE.search(real)
        if not m or m.group(2) in seen:
            continue        # grounded, but not an X post: not an X lead
        sid = m.group(2)
        seen.add(sid)
        p = by_id.get(sid, {})
        uname = m.group(1)
        url = f"https://x.com/{uname}/status/{sid}"
        text = await x_oembed_text(url) or str(p.get("text", ""))[:2000]
        out.append(RawLead("x", url, "", text, project=str(p.get("name", ""))[:200],
                           posted_at=_parse_dt(p.get("date")), author_x=uname,
                           author_name=str(p.get("name", ""))[:200], checked=True))   # grounded = checked
        if len(out) >= max_results:
            break
    return out


# ------------------------------------------------------------------ X
X_ROLE_QUERY = ('(hiring OR "we are hiring" OR "looking for" OR "we\'re looking") '
                '(moderator OR "community manager" OR "customer support" OR "social media manager" '
                'OR "business development" OR ambassador OR "community lead") '
                '(web3 OR crypto OR defi OR nft OR blockchain) -is:retweet lang:en')

_STATUS_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")


async def _x_api(q: str, max_results: int) -> list[RawLead]:
    st = get_settings()
    async with httpx.AsyncClient(timeout=25) as c:
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


async def _twitterapi_io(q: str, max_results: int) -> list[RawLead]:
    st = get_settings()
    async with httpx.AsyncClient(timeout=25) as c:
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


async def x_search(query: str = "", max_results: int = 20, errors: list[str] | None = None) -> list[RawLead]:
    """X API > TwitterAPI.io > Gemini grounding (site:x.com) > ChatGPT web search. First that works wins."""
    errs: list[str] = [] if errors is None else errors
    q = query or X_ROLE_QUERY
    chain = x_providers()
    if not chain:
        raise ProviderError("No X search configured (X_BEARER_TOKEN, TWITTERAPI_IO_KEY or a Gemini LLM_API_KEY)")
    for name in chain:
        if name == "x_api":
            res = await _guarded(name, lambda: _x_api(q, max_results), errs)
        elif name == "twitterapi_io":
            res = await _guarded(name, lambda: _twitterapi_io(q, max_results), errs)
        elif name == "gemini_x":
            res = await _guarded(name, lambda: gemini_x_search(query, max_results), errs)
        else:
            res = await _guarded(name, lambda: openai_x_search(query, max_results), errs)
        if res is not None:
            return res
    raise ProviderError("; ".join(errs) or "All X search providers failed")


OPENAI_X_PROMPT = """Use web search on x.com to find recent X posts (last {days} days) where a Web3 / crypto project
or a person is HIRING for one of: moderator, community manager, customer support, social media manager, business
development / growth, community operations, ambassador or contributor roles. {extra}
Only include posts you actually found with the search tool. Never invent posts, handles or links.
Return ONLY JSON: {{"posts": [{{"url": "https://x.com/<handle>/status/<id>", "author": "<handle without @>",
"name": "<display name>", "text": "<the post text, verbatim>", "date": "YYYY-MM-DD", "author_bio": "<bio if seen>"}}]}}
Up to {n} posts, newest first."""


def _cited_status_ids(data: dict) -> set[str]:
    """Status ids of every URL the search tool actually returned (citations + url_citation annotations)."""
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


def _responses_text(data: dict) -> str:
    if data.get("output_text"):
        return data["output_text"]
    out = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    out.append(part.get("text", ""))
    return "\n".join(out)


async def openai_x_search(query: str = "", max_results: int = 20, days: int = 14) -> list[RawLead]:
    """Optional: ChatGPT's web_search tool limited to x.com. Keeps only posts the tool really cited."""
    st = get_settings()
    prompt = OPENAI_X_PROMPT.format(days=days, n=max_results,
                                    extra=f"Focus on: {query}" if query and query != X_ROLE_QUERY else "")
    body = {"model": st.openai_model, "input": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search", "filters": {"allowed_domains": ["x.com", "twitter.com"]}}]}
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post("https://api.openai.com/v1/responses", json=body,
                         headers={"Authorization": f"Bearer {st.openai_api_key}"})
    if r.status_code != 200:
        raise ProviderError(f"ChatGPT X search HTTP {r.status_code}")
    data = r.json()
    cited = _cited_status_ids(data)
    posts = _json_items(_responses_text(data), "posts")
    out, seen = [], set()
    for p in posts[:max_results]:
        mm = _STATUS_RE.search(str(p.get("url", "")))
        if not mm or mm.group(2) in seen:
            continue
        sid = mm.group(2)
        seen.add(sid)
        if sid not in cited:
            continue   # the model named a post its search never returned: drop it
        uname = (p.get("author") or mm.group(1)).lstrip("@")
        text = str(p.get("text", ""))[:4000]
        if p.get("author_bio"):
            text += "\n\nAuthor bio: " + str(p["author_bio"])[:500]
        out.append(RawLead("x", f"https://x.com/{uname}/status/{sid}", "", text,
                           project=str(p.get("name", "")), posted_at=_parse_dt(p.get("date")),
                           author_x=uname, author_name=str(p.get("name", "")), checked=True))
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
