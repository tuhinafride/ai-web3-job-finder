"""Opportunity qualification: role, pay, contacts, freshness, scam signals, relevance.

Rules always run (deterministic, testable). If an LLM is configured it refines the
structured fields, but scam flags from the rules can never be removed by the LLM.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..classify import WEB3_KEYWORDS
from ..db import utcnow
from . import llm

# ------------------------------------------------------------------ roles
ROLE_PATTERNS: list[tuple[str, list[str]]] = [
    ("super_moderator", [r"super\s*-?\s*mod", r"head\s+(of\s+)?moderat", r"lead\s+moderator", r"senior\s+moderator"]),
    ("moderator", [r"\bmoderators?\b", r"\bmods?\b(?!ule|el|ern|e\b)", r"moderation"]),
    ("community_ops", [r"community\s+op(eration)?s", r"community\s+coordinator", r"community\s+associate"]),
    ("community", [r"community\s+(manager|lead|head|management)", r"\bcm\b", r"telegram\s+manager", r"discord\s+manager"]),
    ("support", [r"customer\s+(support|success|service)", r"support\s+(specialist|agent|rep|associate)", r"\bhelp\s*desk"]),
    ("social", [r"social\s+media", r"\bx\s+manager", r"twitter\s+manager", r"content\s+(creator|manager)"]),
    ("bd_growth", [r"business\s+development", r"\bbd\b", r"\bgrowth\b", r"partnerships?", r"\bkol\s+manager"]),
    ("ambassador", [r"ambassador", r"paid\s+contributor", r"contributors?\s+program", r"bounty", r"campus\s+lead"]),
]

HIRING_PATTERNS = [r"\bhiring\b", r"apply\s+(now|for\s+this|here)", r"\bresponsibilities\b", r"\bqualifications\b",
                   r"what\s+you('|\s+wi)ll\s+do", r"about\s+the\s+role", r"job\s+description", r"we('| a)re\s+looking\s+for", r"looking\s+for\s+(a|an|experienced)",
                   r"\bvacanc", r"join\s+(our|the)\s+team", r"open\s+(role|position)", r"\bapply\b",
                   r"job\s+(opening|opportunit)", r"\bwe\s+need\b", r"\brecruiting\b", r"dm\s+(me|us)"]

# ------------------------------------------------------------------ scam signals (never removed)
SCAM_RULES: list[tuple[str, str, str]] = [
    (r"seed\s*phrase|recovery\s*phrase|mnemonic|12\s*words|24\s*words", "high", "Mentions seed/recovery phrase"),
    (r"private\s*key", "high", "Mentions private keys"),
    (r"(registration|training|activation|application|onboarding|joining)\s+fee", "high", "Asks for an upfront fee"),
    (r"(pay|deposit|send)\s+(a\s+)?(small\s+)?(fee|deposit|usdt|eth|bnb|\$\d+)", "high", "Asks you to pay or deposit"),
    (r"connect\s+(your\s+)?wallet\s+to\s+(apply|verify|register|get)", "high", "Asks to connect a wallet to apply"),
    (r"buy\s+(our\s+)?(token|nft)s?\s+to\s+(start|apply|join|qualify)", "high", "Requires buying tokens/NFTs"),
    (r"guaranteed\s+(income|profit|returns?)|double\s+your", "high", "Guaranteed income/profit claims"),
    (r"whatsapp\s+only|contact\s+(me\s+)?on\s+whatsapp", "medium", "Pushes contact to WhatsApp"),
    (r"(earn|make)\s+\$?\d{3,}(,\d{3})?\s*(per|a|/)\s*(day|hour)", "medium", "Unrealistic pay claim"),
    (r"no\s+(experience|interview)\s+(needed|required).{0,40}\$\d", "medium", "No-experience high-pay pitch"),
    (r"investment\s+(required|needed)|invest\s+(first|to\s+start)", "high", "Requires an investment"),
    (r"verify\s+your\s+(identity|account)\s+(via|with)\s+(link|bot)", "medium", "Verification via link/bot"),
]

PAID_POS = [r"\$\s?\d", r"\d+\s?(k|usd|usdt|usdc)\b", r"\bsalary\b", r"\bpaid\b(?!\s+in\s+exposure)", r"\bcompensation\b",
            r"per\s+(month|hour|week)", r"/\s?(mo|month|hr|hour)\b", r"monthly\s+(pay|stipend)", r"\bstipend\b", r"full[-\s]?time", r"part[-\s]?time"]
UNPAID = [r"\bunpaid\b", r"\bvolunteer", r"for\s+exposure", r"\bno\s+pay\b", r"\bwhitelist\s+(spot|role)s?\s+as\s+(reward|payment)",
          r"paid\s+in\s+(points|xp|whitelist)"]
REMOTE = [r"\bremote\b", r"work\s+from\s+(home|anywhere)", r"\banywhere\b", r"\bglobal\b", r"worldwide", r"international"]

TG_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?!joinchat|\+|s/|c/|share)([A-Za-z][A-Za-z0-9_]{4,31})(?:/\d+)?", re.I)
TG_AT = re.compile(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
X_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?:[/?]|\b)", re.I)
DEADLINE = re.compile(r"(?:deadline|apply\s+by|closes?\s+(?:on)?|until)\s*[:\-]?\s*([A-Z]?[a-z]{2,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)", re.I)
PAY_DETAIL = re.compile(r"(\$\s?\d[\d,.]*\s?(?:k)?(?:\s?[-–]\s?\$?\s?\d[\d,.]*\s?k?)?(?:\s?(?:/|per)\s?(?:month|mo|hour|hr|week|year|yr))?|\d[\d,.]*\s?(?:usdt|usdc|usd)(?:\s?(?:/|per)\s?(?:month|mo|hour|week))?)", re.I)

NOT_HANDLES = {"gmail", "everyone", "here", "channel", "admin", "admins", "support", "telegram", "discord",
               "twitter", "username", "mention", "yourname", "hiring", "jobs", "media"}


@dataclass
class Qualified:
    project: str = ""
    title: str = ""
    role: str = "other"
    summary: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    pay_status: str = "unknown"
    pay_detail: str = ""
    apply_method: str = ""
    deadline: str = ""
    remote: str = "unknown"
    is_hiring: bool = False
    web3: bool = False
    scam_risk: str = "low"
    scam_flags: list[str] = field(default_factory=list)
    freshness: str = "unknown"
    relevance: int = 0
    contacts: list[dict] = field(default_factory=list)   # {platform, handle, role_hint}
    ai_used: bool = False


def lead_uid(source_url: str, text: str = "") -> str:
    u = (source_url or "").lower().split("#")[0].rstrip("/")
    u = re.sub(r"[?&](utm_[^=]+|ref|s|t)=[^&]*", "", u)
    u = u.replace("twitter.com/", "x.com/").replace("://www.", "://")
    if not u:
        u = re.sub(r"\W+", " ", text.lower()).strip()[:300]
    return hashlib.sha1(u.encode()).hexdigest()


def text_fingerprint(text: str) -> str:
    """Near-duplicate key: same announcement reposted under different URLs."""
    words = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    if len(set(words)) < 15:
        return ""
    return hashlib.sha1(" ".join(sorted(set(words))[:60]).encode()).hexdigest()


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def detect_role(text: str) -> str:
    """Role whose keywords appear EARLIEST in the text (titles come first), super-mod wins ties."""
    best, best_pos = "other", 10**9
    for key, pats in ROLE_PATTERNS:
        for p in pats:
            m = re.search(p, text, re.I)
            if m and m.start() < best_pos:
                best, best_pos = key, m.start()
    if best == "moderator" and _any(ROLE_PATTERNS[0][1], text[:best_pos + 80]):
        best = "super_moderator"
    return best


def scam_check(text: str) -> tuple[str, list[str]]:
    flags, risk = [], "low"
    for pat, level, label in SCAM_RULES:
        if re.search(pat, text, re.I):
            flags.append(label)
            if level == "high" or (level == "medium" and risk == "low"):
                risk = level if risk != "high" else "high"
    return risk, flags


def extract_contacts(text: str, author_x: str = "", author_tg: str = "") -> list[dict]:
    out: list[dict] = []
    seen = set()

    def add(platform, handle, hint):
        h = handle.strip().lstrip("@")
        key = (platform, h.lower())
        if not h or key in seen or h.lower() in NOT_HANDLES:
            return
        if platform in ("telegram", "x") and (h.lower().endswith("bot") or h.lower() in NOT_HANDLES):
            return
        seen.add(key)
        out.append({"platform": platform, "handle": h, "role_hint": hint})

    if author_tg:
        add("telegram", author_tg, "post author")
    if author_x:
        add("x", author_x, "post author")
    for m in TG_LINK.finditer(text):
        add("telegram", m.group(1), "listed in post")
    for m in X_LINK.finditer(text):
        if m.group(1).lower() not in ("i", "home", "search", "intent", "share", "hashtag"):
            add("x", m.group(1), "listed in post")
    for m in EMAIL.finditer(text):
        e = m.group(0)
        if not e.lower().endswith((".png", ".jpg", ".gif", ".webp")):
            add("email", e.lower(), "listed in post")
    # bare @handles: on X they are X users; elsewhere usually Telegram usernames
    plat = "x" if author_x else "telegram"
    for m in TG_AT.finditer(text):
        add(plat, m.group(1), "mentioned in post")
    return out[:8]


def freshness_of(posted_at: datetime | None, max_days: int = 30) -> str:
    if not posted_at:
        return "unknown"
    return "current" if utcnow() - posted_at <= timedelta(days=max_days) else "old"


def score(q: Qualified, preferred_roles: list[str], posted_at: datetime | None) -> int:
    s = 0
    if q.role in preferred_roles:
        s += 40
    elif q.role != "other":
        s += 20
    s += {"paid": 20, "unknown": 8, "unpaid": 0}[q.pay_status]
    if q.remote == "yes":
        s += 10
    if q.is_hiring:
        s += 10
    if q.web3:
        s += 8
    if any(c["platform"] in ("telegram", "email") for c in q.contacts):
        s += 4
    if posted_at:
        age = (utcnow() - posted_at).days
        s += 8 if age <= 7 else 4 if age <= 30 else -15
    s -= {"low": 0, "medium": 25, "high": 60}[q.scam_risk]
    return max(0, min(100, s))


def rule_qualify(text: str, title: str = "", project: str = "", posted_at: datetime | None = None,
                 author_x: str = "", author_tg: str = "", preferred_roles: list[str] | None = None) -> Qualified:
    full = f"{title}\n{text}"
    low = full.lower()
    if not project and title:
        m = re.search(r"\bat\s+([A-Z0-9][\w.&' -]{1,40}?)\s*(?:[-|–(]|$)", title)
        project = m.group(1) if m else ""
    if not project:
        m = re.search(r"\b([A-Z][\w.]+(?:\s[A-Z][\w.]+)?)\s+(?:\([^)]{1,30}\)\s+)?(?:is\s+|are\s+)?(?:hiring|looking\s+for)",
                      f"{title}\n{text[:400]}")
        if m and m.group(1).lower() not in ("we", "we're", "now", "urgently", "hiring", "currently", "our"):
            project = m.group(1)
    q = Qualified(project=project.strip()[:200], title=re.split(r"\s[-|–]\s", title)[0].strip()[:300] if title else "")
    q.role = detect_role(title) if title and detect_role(title) != "other" else detect_role(full)
    q.is_hiring = _any(HIRING_PATTERNS, full)
    q.web3 = any(k in low for k in WEB3_KEYWORDS)
    if _any(UNPAID, full):
        q.pay_status = "unpaid"
    elif _any(PAID_POS, full):
        q.pay_status = "paid"
    m = PAY_DETAIL.search(full)
    if m and q.pay_status != "unpaid":
        q.pay_detail = m.group(1).strip()[:120]
        q.pay_status = "paid"
    q.remote = "yes" if _any(REMOTE, full) else "unknown"
    d = DEADLINE.search(full)
    q.deadline = d.group(1)[:60] if d else ""
    q.scam_risk, q.scam_flags = scam_check(full)
    q.contacts = extract_contacts(text, author_x, author_tg)
    if re.search(r"\bapply\b.{0,60}(https?://\S+)", full, re.I):
        q.apply_method = re.search(r"\bapply\b.{0,60}(https?://\S+)", full, re.I).group(1)[:300]
    elif re.search(r"\bdm\b|send\s+(your\s+)?(cv|resume|portfolio)", full, re.I):
        q.apply_method = "Send a DM / CV as described in the post"
    bullets = [ln.strip(" •-*\t") for ln in text.split("\n") if re.match(r"\s*[•\-*]\s+\S", ln)]
    q.responsibilities = bullets[:6]
    q.summary = re.sub(r"\s+", " ", text).strip()[:400]
    if not q.title:
        q.title = {"other": "Web3 opportunity"}.get(q.role) or _role_title(q.role)
    q.freshness = freshness_of(posted_at)
    q.relevance = score(q, preferred_roles or [], posted_at)
    return q


def _role_title(role: str) -> str:
    from .models import ROLE_LABELS
    return ROLE_LABELS.get(role, "Web3 opportunity")


QUALIFY_SYSTEM = """You analyse Web3/crypto job posts for a job seeker. Return ONLY a JSON object with keys:
project (string), title (string), role (one of: super_moderator, moderator, community, support, social,
bd_growth, community_ops, ambassador, other), is_hiring (bool: is this an actual open vacancy/hiring call,
not a job seeker or news), pay_status (paid|unpaid|unknown), pay_detail (string, only if stated),
remote (yes|no|unknown), deadline (string, only if stated), apply_method (string, only if stated),
responsibilities (array of <=5 short strings), requirements (array of <=5 short strings),
summary (<=40 words), extra_scam_flags (array of short strings for anything suspicious: upfront payments,
seed phrases, private keys, fake recruiters, wallet connection to apply, unrealistic pay, impersonation).
Use only facts stated in the text. Use "" or "unknown" when not stated. Never invent."""


async def qualify(text: str, title: str = "", project: str = "", posted_at: datetime | None = None,
                  author_x: str = "", author_tg: str = "", preferred_roles: list[str] | None = None,
                  use_llm: bool = True) -> Qualified:
    q = rule_qualify(text, title, project, posted_at, author_x, author_tg, preferred_roles)
    if not (use_llm and llm.enabled()):
        return q
    try:
        data = await llm.chat_json(QUALIFY_SYSTEM, f"Title: {title}\nProject: {project}\n\n{text[:5000]}")
    except llm.LLMUnavailable:
        return q
    from .models import ROLE_LABELS
    q.ai_used = True
    q.project = str(data.get("project") or q.project)[:200]
    q.title = str(data.get("title") or q.title)[:300]
    if data.get("role") in ROLE_LABELS:
        q.role = data["role"]
    if isinstance(data.get("is_hiring"), bool):
        q.is_hiring = data["is_hiring"]
    if data.get("pay_status") in ("paid", "unpaid", "unknown") and not (q.pay_status == "paid" and data["pay_status"] == "unknown"):
        q.pay_status = data["pay_status"]
    q.pay_detail = str(data.get("pay_detail") or q.pay_detail)[:120]
    if data.get("remote") in ("yes", "no", "unknown") and data["remote"] != "unknown":
        q.remote = data["remote"]
    q.deadline = str(data.get("deadline") or q.deadline)[:60]
    q.apply_method = str(data.get("apply_method") or q.apply_method)[:300]
    for key in ("responsibilities", "requirements"):
        v = data.get(key)
        if isinstance(v, list) and v:
            setattr(q, key, [str(x)[:200] for x in v[:5]])
    q.summary = str(data.get("summary") or q.summary)[:400]
    extra = [str(x)[:120] for x in (data.get("extra_scam_flags") or []) if str(x).strip()][:4]
    if extra:
        q.scam_flags += [f"AI: {x}" for x in extra]
        if q.scam_risk == "low":
            q.scam_risk = "medium"
    q.relevance = score(q, preferred_roles or [], posted_at)
    return q
