"""Text helpers: HTML cleanup, role classification, requirement extraction, Web3 detection."""
from __future__ import annotations

import html
import re

# Category key -> (label, keywords matched against the job title first, then tags)
CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "community": ("Community Manager", ["community", "ambassador", "discord", "telegram manager", "devrel community"]),
    "moderator": ("Moderator", ["moderator", "moderation", "mod "]),
    "social": ("Social Media", ["social media", "content creator", "content", "twitter", "copywriter", "kol", "influencer", "brand"]),
    "support": ("Customer Support", ["support", "customer success", "customer service", "help desk", "cs agent", "customer experience"]),
    "bd": ("Business Development", ["business development", " bd ", "bd ", "partnership", "sales", "account executive", "account manager", "listing"]),
    "growth": ("Growth & Marketing", ["growth", "marketing", "seo", "acquisition", "campaign", "events", "pr ", "communications"]),
    "developer": ("Developer / Engineer", ["engineer", "developer", "solidity", "rust", "smart contract", "backend", "frontend", "full stack", "fullstack", "devops", "sre", "blockchain dev", "protocol", "security researcher", "auditor", "data scientist", "machine learning", "qa "]),
    "design": ("Design / Product", ["designer", "design", "product manager", "product owner", "ux", "ui "]),
    "research": ("Research & Analyst", ["research", "analyst", "economist", "trader", "quant"]),
    "ops": ("Operations / Compliance / HR", ["operations", "compliance", "legal", "counsel", "kyc", "aml", "risk", "recruit", "talent", "hr ", "people", "finance", "accountant", "treasury", "admin"]),
}
CATEGORY_LABELS = {k: v[0] for k, v in CATEGORIES.items()} | {"other": "Other crypto roles"}

WEB3_KEYWORDS = [
    "web3", "crypto", "blockchain", "defi", "nft", "dao", "token", "solidity", "ethereum",
    "bitcoin", "solana", "onchain", "on-chain", "smart contract", "stablecoin", "exchange",
    "wallet", "layer 2", "l2", "zk", "polygon", "binance", "bnb", "dex", "staking",
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)          # greenhouse double-escapes content
    text = html.unescape(text)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h\d|ul|ol)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", text)
    text = _TAG_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def classify(title: str, tags: str = "") -> str:
    t = f" {title.lower()} "
    for key, (_, kws) in CATEGORIES.items():
        if any(kw in t for kw in kws):
            return key
    tg = f" {tags.lower()} "
    for key, (_, kws) in CATEGORIES.items():
        if any(kw in tg for kw in kws):
            return key
    return "other"


_REQ_HEAD = re.compile(
    r"(?i)(requirements|qualifications|what you('| wi)ll? bring|what we('re| are) looking for|"
    r"you (have|bring|are)|about you|who you are|must have|skills|experience)"
)


def extract_requirements(text: str, limit: int = 6) -> list[str]:
    """Pull the bullet points that follow a 'Requirements'-style heading; fall back to none."""
    if not text:
        return []
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if _REQ_HEAD.search(ln) and len(ln) < 90 and not ln.startswith("•"):
            bullets = []
            for nxt in lines[i + 1:i + 40]:
                if nxt.startswith("•"):
                    b = nxt.lstrip("• ").strip()
                    if 3 < len(b) < 300:
                        bullets.append(b)
                    if len(bullets) >= limit:
                        break
                elif bullets and nxt and not nxt.startswith("•") and len(nxt) < 90:
                    break  # next heading
            if bullets:
                return bullets
    return []


def is_web3(*fields: str) -> bool:
    blob = " ".join(f for f in fields if f).lower()
    return any(re.search(rf"\b{re.escape(k)}\b", blob) for k in WEB3_KEYWORDS)


def detect_remote(*fields: str) -> bool:
    blob = " ".join(f for f in fields if f).lower()
    return any(k in blob for k in ("remote", "anywhere", "worldwide", "work from home"))


def trim(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_SALARY_RE = re.compile(
    r"(?:US\$|USD\s?|\$|€|£)\s?\d{2,3}(?:[,.]\d{3})*(?:\.\d+)?\s?[kK]?"
    r"\s?(?:-|–|—|to)\s?(?:US\$|USD\s?|\$|€|£)?\s?\d{2,3}(?:[,.]\d{3})*(?:\.\d+)?\s?[kK]?"
    r"(?:\s?(?:USD|EUR|GBP))?(?:\s?(?:per|/|a)\s?(?:year|yr|annum|hour|hr|month))?"
)


def extract_salary(text: str) -> str:
    """Find a pay range the employer wrote in the description (e.g. pay-transparency sections)."""
    if not text:
        return ""
    m = _SALARY_RE.search(text)
    return m.group(0).strip() if m else ""
