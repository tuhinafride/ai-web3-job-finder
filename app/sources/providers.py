"""Real job sources. Every listing comes from a public, documented job feed.

No listing is ever invented. Each job records where it came from:
  * company_ats  -> fetched straight from the employer's own careers board
                    (Greenhouse / Lever / Ashby public APIs). Shown as
                    "Direct from company careers page".
  * aggregator   -> third-party job board (Remote OK, Remotive, Cryptocurrency Jobs). Shown as
                    "Third-party listing: verify on the company's site before applying".
Neither label claims a job is "legit"; it only states the source.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ..classify import (classify, detect_remote, extract_requirements, extract_salary, html_to_text,
                        is_web3, trim)

log = logging.getLogger(__name__)
UA = {"User-Agent": "Web3JobFinderBot/1.0 (+https://core.telegram.org/bots)"}

# Crypto employers with public careers-board APIs (verified working Oct 2026).
# Add more: (board slug, display name). Find the slug in the company's careers URL.
GREENHOUSE_BOARDS = [
    ("consensys", "Consensys"), ("coinbase", "Coinbase"), ("gemini", "Gemini"),
    ("ripple", "Ripple"), ("fireblocks", "Fireblocks"), ("okx", "OKX"), ("bitgo", "BitGo"),
]
LEVER_BOARDS = [("binance", "Binance"), ("1inch", "1inch")]
ASHBY_BOARDS = [
    ("kraken.com", "Kraken"), ("uniswap", "Uniswap Labs"), ("phantom", "Phantom"),
    ("polygon-labs", "Polygon Labs"), ("paradigm", "Paradigm (portfolio)"),
]


@dataclass
class RawJob:
    source: str
    source_name: str
    source_type: str
    external_id: str
    title: str
    company: str
    url: str
    apply_url: str
    location: str = ""
    remote: bool = False
    salary: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    posted_at: datetime | None = None

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.external_id}"

    def to_row(self) -> dict:
        tags = ",".join(t.strip().lower() for t in self.tags if t and t.strip())[:2000]
        desc = self.description or ""
        return dict(
            uid=self.uid, source=self.source, source_name=self.source_name,
            source_type=self.source_type, title=trim(self.title.strip(), 300),
            company=trim((self.company or "Unknown").strip(), 200),
            location=trim(self.location or "", 300),
            remote=self.remote or detect_remote(self.location, self.title),
            salary=trim(self.salary or extract_salary(desc), 200),
            category=classify(self.title, tags), tags=tags,
            requirements="\n".join(extract_requirements(desc)),
            description=trim(desc, 6000), url=self.url, apply_url=self.apply_url or self.url,
            posted_at=self.posted_at,
        )


def _ts(value) -> datetime | None:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _money(lo, hi, cur="USD", period="year") -> str:
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    sym = "$" if (cur or "USD").upper() == "USD" else f"{cur} "
    if lo and hi and lo != hi:
        return f"{sym}{lo:,}–{sym}{hi:,} / {period}"
    return f"{sym}{(hi or lo):,} / {period}"


async def _get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


# ---------------- company ATS boards ----------------
async def fetch_greenhouse(client, slug, name) -> list[RawJob]:
    data = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        desc = html_to_text(j.get("content", ""))
        meta_salary = ""
        for m in j.get("metadata") or []:
            if "salary" in (m.get("name") or "").lower() or "compensation" in (m.get("name") or "").lower():
                v = m.get("value")
                if isinstance(v, dict):
                    meta_salary = _money(v.get("min_value"), v.get("max_value"), v.get("unit") or "USD")
                elif v:
                    meta_salary = str(v)
        loc = (j.get("location") or {}).get("name", "")
        out.append(RawJob(
            source=f"greenhouse-{slug}", source_name=f"{name} careers (Greenhouse)",
            source_type="company_ats", external_id=str(j["id"]), title=j.get("title", ""),
            company=j.get("company_name") or name, url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""), location=loc, salary=meta_salary,
            tags=[d.get("name", "") for d in j.get("departments") or []], description=desc,
            posted_at=_ts(j.get("first_published") or j.get("updated_at")),
        ))
    return out


async def fetch_lever(client, slug, name) -> list[RawJob]:
    data = await _get_json(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content", "")))  # already <li> items
        desc = "\n".join(p for p in parts if p)
        cats = j.get("categories") or {}
        sr = j.get("salaryRange") or {}
        out.append(RawJob(
            source=f"lever-{slug}", source_name=f"{name} careers (Lever)", source_type="company_ats",
            external_id=j["id"], title=j.get("text", ""), company=name,
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
            location=cats.get("location") or ", ".join(cats.get("allLocations") or []),
            remote=(j.get("workplaceType") == "remote"),
            salary=_money(sr.get("min"), sr.get("max"), sr.get("currency") or "USD",
                          (sr.get("interval") or "per-year").replace("per-", "")) if sr else "",
            tags=[cats.get("team", ""), cats.get("department", ""), cats.get("commitment", "")],
            description=desc, posted_at=_ts(j.get("createdAt")),
        ))
    return out


async def fetch_ashby(client, slug, name) -> list[RawJob]:
    data = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        salary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or ""
        out.append(RawJob(
            source=f"ashby-{slug}", source_name=f"{name} careers (Ashby)", source_type="company_ats",
            external_id=j["id"], title=j.get("title", ""), company=name,
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            location=j.get("location", ""), remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            salary=salary or "", tags=[j.get("department", ""), j.get("team", ""), j.get("employmentType", "")],
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            posted_at=_ts(j.get("publishedAt")),
        ))
    return out


# ---------------- aggregators ----------------
async def fetch_remoteok(client) -> list[RawJob]:
    out = []
    for tag in ("web3", "crypto", "blockchain"):
        data = await _get_json(client, f"https://remoteok.com/api?tag={tag}")
        for j in data[1:] if isinstance(data, list) else []:
            if not j.get("id"):
                continue
            desc = html_to_text(j.get("description", ""))
            out.append(RawJob(
                source="remoteok", source_name="Remote OK (third-party board)", source_type="aggregator",
                external_id=str(j["id"]), title=j.get("position", ""), company=j.get("company", ""),
                url=j.get("url", ""), apply_url=j.get("apply_url") or j.get("url", ""),
                location=j.get("location") or "Remote", remote=True,
                salary=_money(j.get("salary_min"), j.get("salary_max")), tags=j.get("tags") or [],
                description=desc, posted_at=_ts(j.get("epoch") or j.get("date")),
            ))
        await asyncio.sleep(1)  # be polite
    return out


async def fetch_remotive(client) -> list[RawJob]:
    out = []
    for q in ("web3", "crypto", "blockchain"):
        data = await _get_json(client, f"https://remotive.com/api/remote-jobs?search={q}")
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("description", ""))
            if not is_web3(j.get("title", ""), j.get("company_name", ""), " ".join(j.get("tags") or []), desc[:3000]):
                continue  # Remotive search is fuzzy; keep only genuinely crypto-related posts
            out.append(RawJob(
                source="remotive", source_name="Remotive (third-party board)", source_type="aggregator",
                external_id=str(j["id"]), title=j.get("title", ""), company=j.get("company_name", ""),
                url=j.get("url", ""), apply_url=j.get("url", ""),
                location=j.get("candidate_required_location") or "Remote", remote=True,
                salary=j.get("salary") or "", tags=(j.get("tags") or []) + [j.get("category", "")],
                description=desc, posted_at=_ts(j.get("publication_date")),
            ))
    return out


async def fetch_cryptocurrencyjobs(client) -> list[RawJob]:
    """cryptocurrencyjobs.co public RSS feed (title is "Role at Company")."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    r = await client.get("https://cryptocurrencyjobs.co/index.xml", headers=UA, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        raw_title = html_to_text(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        if " at " not in raw_title or not link.startswith("https://"):
            continue
        title, company = raw_title.rsplit(" at ", 1)
        desc = html_to_text(it.findtext("description") or "")
        try:
            posted = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            posted = None
        out.append(RawJob(
            source="cryptocurrencyjobs", source_name="Cryptocurrency Jobs (third-party board)",
            source_type="aggregator", external_id=link.rstrip("/").rsplit("/", 1)[-1], title=title,
            company=company, url=link, apply_url=link, location="Remote" if detect_remote(desc) else "",
            remote=detect_remote(desc), tags=[link.split("/")[3] if link.count("/") > 3 else ""],
            description=desc, posted_at=posted,
        ))
    return out


async def fetch_all() -> tuple[list[RawJob], dict[str, str]]:
    """Fetch every source concurrently. Returns (jobs, status per source)."""
    status: dict[str, str] = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks: dict[str, asyncio.Task] = {}
        for slug, name in GREENHOUSE_BOARDS:
            tasks[f"greenhouse:{slug}"] = asyncio.create_task(fetch_greenhouse(client, slug, name))
        for slug, name in LEVER_BOARDS:
            tasks[f"lever:{slug}"] = asyncio.create_task(fetch_lever(client, slug, name))
        for slug, name in ASHBY_BOARDS:
            tasks[f"ashby:{slug}"] = asyncio.create_task(fetch_ashby(client, slug, name))
        tasks["remoteok"] = asyncio.create_task(fetch_remoteok(client))
        tasks["remotive"] = asyncio.create_task(fetch_remotive(client))
        tasks["cryptocurrencyjobs"] = asyncio.create_task(fetch_cryptocurrencyjobs(client))
        jobs: list[RawJob] = []
        for key, t in tasks.items():
            try:
                res = await t
                jobs.extend(j for j in res if j.title and j.url)
                status[key] = f"ok ({len(res)})"
            except Exception as e:  # one broken source must not stop the rest
                log.warning("source %s failed: %s", key, e)
                status[key] = f"error: {type(e).__name__}"
    # de-duplicate by uid
    seen, unique = set(), []
    for j in jobs:
        if j.uid not in seen:
            seen.add(j.uid)
            unique.append(j)
    return unique, status
