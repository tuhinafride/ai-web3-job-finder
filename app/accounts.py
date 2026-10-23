"""Users, referrals, reward settings, daily usage limits and the admin audit log.

Identity always comes from Telegram (bot updates or HMAC-verified Mini App initData). This
module never trusts a client-sent user id or admin flag; callers pass the verified user.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import (AdminAuditLog, AppSetting, Referral, ReferralEvent, UsageCounter, User, UserActivity,
                 utcnow)

log = logging.getLogger("accounts")

ACTIVE, SUSPENDED = "active", "suspended"
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o/1/l/i
CODE_RE = re.compile(r"^[a-z0-9]{6,16}$")
_BOT = {"username": ""}


class BotAccountRejected(Exception):
    pass


class LimitReached(Exception):
    def __init__(self, kind: str, limit: int):
        super().__init__(kind)
        self.kind, self.limit = kind, limit


# ------------------------------------------------------------------ helpers
def today() -> date:
    return utcnow().date()


def is_admin_id(user_id: int | None) -> bool:
    return bool(user_id) and int(user_id) in get_settings().admin_ids


def set_bot_username(name: str | None):
    _BOT["username"] = (name or "").lstrip("@")


def bot_username() -> str:
    return _BOT["username"] or get_settings().bot_username_env.strip().lstrip("@")


def display_name(u: User | None) -> str:
    if u is None:
        return "Someone"
    if u.username:
        return "@" + u.username
    return (u.first_name or "").strip()[:40] or "Anonymous"


def csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @, tab, CR at the start)."""
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ")
    if s and s[0] in ("=", "+", "-", "@", "\t"):
        s = "'" + s
    return s


def iso(dt) -> str | None:
    return dt.isoformat() + "Z" if isinstance(dt, datetime) else (dt.isoformat() if dt else None)


async def insert_ignore(s: AsyncSession, model, **values) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING (Postgres and SQLite). True when a row was inserted."""
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    res = await s.execute(insert(model).values(**values).on_conflict_do_nothing())
    return (res.rowcount or 0) > 0


# ------------------------------------------------------------------ referral codes / links
def new_code(n: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def parse_ref(param: str | None) -> str | None:
    p = (param or "").strip().lower()
    if not p.startswith("ref_"):
        return None
    code = p[4:]
    return code if CODE_RE.match(code) else None


async def ensure_code(s: AsyncSession, u: User) -> str:
    if u.referral_code:
        return u.referral_code
    for _ in range(8):
        code = new_code()
        if not await s.scalar(select(User.tg_id).where(User.referral_code == code)):
            u.referral_code = code
            await s.flush()
            s.add(ReferralEvent(referrer_id=u.tg_id, user_id=u.tg_id, kind="link_created"))
            return code
    raise RuntimeError("could not allocate referral code")


def referral_links(code: str | None) -> dict:
    bot = bot_username()
    if not code or not bot:
        return {"bot_link": "", "app_link": "", "share_url": "", "bot_username": bot}
    bot_link = f"https://t.me/{bot}?start=ref_{code}"
    short = get_settings().miniapp_short_name.strip().strip("/")
    app_link = f"https://t.me/{bot}/{short}?startapp=ref_{code}" if short else ""
    from urllib.parse import quote
    text = "Find real Web3 jobs with this Telegram bot. Join with my invite link:"
    share = f"https://t.me/share/url?url={quote(app_link or bot_link, safe='')}&text={quote(text, safe='')}"
    return {"bot_link": bot_link, "app_link": app_link, "share_url": share, "bot_username": bot}


# ------------------------------------------------------------------ app settings (reward config)
REFERRAL_DEFAULTS = {"referral_enabled": "true", "referral_points": "10"}


async def get_setting(s: AsyncSession, key: str) -> str:
    row = await s.get(AppSetting, key)
    return row.value if row else REFERRAL_DEFAULTS.get(key, "")


async def set_setting(s: AsyncSession, key: str, value: str):
    row = await s.get(AppSetting, key)
    if row:
        row.value = value
    else:
        s.add(AppSetting(key=key, value=value))
    await s.flush()


async def referral_config(s: AsyncSession) -> dict:
    enabled = (await get_setting(s, "referral_enabled")).lower() == "true"
    try:
        points = max(0, int(await get_setting(s, "referral_points")))
    except ValueError:
        points = 0
    return {"enabled": enabled, "points_per_referral": points,
            "hourly_cap": get_settings().referral_hourly_cap,
            "label": "Reward points only. They have no cash or token value."}


# ------------------------------------------------------------------ users
async def record_activity(s: AsyncSession, user_id: int, kind: str):
    await insert_ignore(s, UserActivity, user_id=user_id, day=today(), kind=kind)


async def upsert_user(s: AsyncSession, tg: dict, channel: str, start_param: str = "") -> tuple[User, bool, str]:
    """Creates or refreshes a user from a VERIFIED Telegram user dict.

    Returns (user, is_new, referral_outcome). Referral attribution only happens on first sight.
    Writes to an existing row are throttled (last_active_at at most once per interval).
    """
    if tg.get("is_bot"):
        raise BotAccountRejected()
    uid = int(tg["id"])
    username = (tg.get("username") or None)
    first_name = (tg.get("first_name") or None)
    username = username[:64] if username else None
    first_name = first_name[:128] if first_name else None
    now = utcnow()
    is_new = False
    outcome = ""
    u = await s.get(User, uid)
    if u is None:
        is_new = await insert_ignore(s, User, tg_id=uid, username=username, first_name=first_name, roles="",
                                     skills="", remote_only=False, alerts_enabled=False, created_at=now,
                                     registered_at=now, last_active_at=now, status=ACTIVE, referral_source=channel,
                                     opened_app_at=now if channel == "miniapp" else None)
        u = await s.get(User, uid)
        if is_new:
            await ensure_code(s, u)
            await record_activity(s, uid, "app" if channel == "miniapp" else "bot")
            code = parse_ref(start_param)
            if code:
                outcome = await attribute(s, u, code, channel)
            return u, True, outcome
    changed = False
    if username is not None and u.username != username:
        u.username, changed = username, True
    if first_name is not None and u.first_name != first_name:
        u.first_name, changed = first_name, True
    if channel == "miniapp" and u.opened_app_at is None:
        u.opened_app_at, changed = now, True
    if u.status is None:
        u.status = ACTIVE
    interval = get_settings().activity_write_interval
    last = u.last_active_at
    if last is None or (now - last).total_seconds() >= interval or last.date() != now.date() or changed:
        u.last_active_at = now
        await record_activity(s, uid, "app" if channel == "miniapp" else "bot")
    if code := parse_ref(start_param):
        outcome = "existing_user"   # existing users are never (re)attributed
        await _event(s, None, uid, "rejected", f"existing_user:{code}")
    return u, is_new, outcome


async def _event(s, referrer_id, user_id, kind, details=""):
    s.add(ReferralEvent(referrer_id=referrer_id, user_id=user_id, kind=kind, details=details[:200]))


async def attribute(s: AsyncSession, new_user: User, code: str, channel: str) -> str:
    referrer = await s.scalar(select(User).where(User.referral_code == code))
    if referrer is None:
        await _event(s, None, new_user.tg_id, "rejected", "unknown_code")
        return "unknown_code"
    if referrer.tg_id == new_user.tg_id:
        await _event(s, referrer.tg_id, new_user.tg_id, "rejected", "self_referral")
        return "self_referral"
    if referrer.status == SUSPENDED:
        await _event(s, referrer.tg_id, new_user.tg_id, "rejected", "referrer_suspended")
        return "referrer_suspended"
    cap = get_settings().referral_hourly_cap
    recent = await s.scalar(select(func.count(Referral.id)).where(
        Referral.referrer_id == referrer.tg_id, Referral.valid.is_(True),
        Referral.created_at >= utcnow() - timedelta(hours=1))) or 0
    valid, reason = True, ""
    if cap > 0 and recent >= cap:
        valid, reason = False, "hourly_cap"
    cfg = await referral_config(s)
    points = cfg["points_per_referral"] if (valid and cfg["enabled"]) else 0
    inserted = await insert_ignore(s, Referral, referrer_id=referrer.tg_id, referred_id=new_user.tg_id,
                                   created_at=utcnow(), valid=valid, reason=reason, points=points, channel=channel)
    if not inserted:
        await _event(s, referrer.tg_id, new_user.tg_id, "rejected", "already_referred")
        return "already_referred"
    new_user.referred_by = referrer.tg_id
    new_user.referral_source = "referral"
    await _event(s, referrer.tg_id, new_user.tg_id, "attributed" if valid else "rejected",
                 "valid" if valid else reason)
    return "valid" if valid else f"invalid:{reason}"


async def referral_stats(s: AsyncSession, user_id: int) -> dict:
    invited = await s.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user_id)) or 0
    valid = await s.scalar(select(func.count(Referral.id)).where(
        Referral.referrer_id == user_id, Referral.valid.is_(True))) or 0
    points = await s.scalar(select(func.coalesce(func.sum(Referral.points), 0)).where(
        Referral.referrer_id == user_id)) or 0
    return {"invited": int(invited), "valid": int(valid), "points": int(points)}


async def leaderboard(s: AsyncSession, limit: int = 10, include_ids: bool = False) -> list[dict]:
    cnt = func.count(Referral.id).label("valid")
    pts = func.coalesce(func.sum(Referral.points), 0).label("points")
    rows = (await s.execute(
        select(User, cnt, pts).join(Referral, Referral.referrer_id == User.tg_id)
        .where(Referral.valid.is_(True), or_(User.status.is_(None), User.status != SUSPENDED))
        .group_by(User.tg_id).order_by(cnt.desc(), pts.desc(), User.tg_id).limit(limit))).all()
    out = []
    for i, (u, v, p) in enumerate(rows, 1):
        row = {"rank": i, "name": display_name(u), "valid": int(v), "points": int(p)}
        if include_ids:
            row["id"] = u.tg_id
        out.append(row)
    return out


# ------------------------------------------------------------------ daily usage limits
def daily_limit(kind: str) -> int:
    st = get_settings()
    return {"search": st.user_daily_searches, "draft": st.user_daily_drafts}.get(kind, 0)


async def usage_today(s: AsyncSession, user_id: int, kind: str) -> int:
    return await s.scalar(select(UsageCounter.count).where(
        UsageCounter.user_id == user_id, UsageCounter.day == today(), UsageCounter.kind == kind)) or 0


async def consume(s: AsyncSession, user_id: int, kind: str) -> int:
    """Counts one paid call. Raises LimitReached for normal users over the daily limit."""
    if is_admin_id(user_id):
        return 0
    limit = daily_limit(kind)
    used = await usage_today(s, user_id, kind)
    if used >= limit:
        raise LimitReached(kind, limit)
    if not await insert_ignore(s, UsageCounter, user_id=user_id, day=today(), kind=kind, count=1):
        row = await s.scalar(select(UsageCounter).where(
            UsageCounter.user_id == user_id, UsageCounter.day == today(), UsageCounter.kind == kind))
        row.count = (row.count or 0) + 1
    return used + 1


async def limits_for(s: AsyncSession, user_id: int) -> dict:
    if is_admin_id(user_id):
        return {"unlimited": True}
    out = {"unlimited": False, "resets": "00:00 UTC"}
    for kind in ("search", "draft"):
        lim, used = daily_limit(kind), await usage_today(s, user_id, kind)
        out[kind] = {"limit": lim, "used": used, "left": max(0, lim - used)}
    return out


# ------------------------------------------------------------------ admin audit
async def audit(s: AsyncSession, admin_id: int, action: str, target: str = "", details: str = ""):
    s.add(AdminAuditLog(admin_id=admin_id, action=action[:48], target=str(target)[:64], details=details[:2000]))


# ------------------------------------------------------------------ admin stats
def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def admin_stats(s: AsyncSession) -> dict:
    t = today()
    distinct_active = lambda since: s.scalar(select(func.count(func.distinct(UserActivity.user_id)))
                                             .where(UserActivity.day >= since))
    reg = func.coalesce(User.registered_at, User.created_at)
    data = {
        "total_users": await s.scalar(select(func.count(User.tg_id))) or 0,
        "opened_app": await s.scalar(select(func.count(User.tg_id)).where(User.opened_app_at.is_not(None))) or 0,
        "dau": await distinct_active(t) or 0,
        "wau": await distinct_active(t - timedelta(days=6)) or 0,
        "mau": await distinct_active(t - timedelta(days=29)) or 0,
        "new_today": await s.scalar(select(func.count(User.tg_id)).where(reg >= _day_start(t))) or 0,
        "suspended": await s.scalar(select(func.count(User.tg_id)).where(User.status == SUSPENDED)) or 0,
        "referral_links": await s.scalar(select(func.count(User.tg_id)).where(User.referral_code.is_not(None))) or 0,
        "referral_invited": await s.scalar(select(func.count(Referral.id))) or 0,
        "referral_registrations": await s.scalar(select(func.count(Referral.id)).where(Referral.valid.is_(True))) or 0,
        "timezone": "UTC",
    }
    start = t - timedelta(days=29)
    active = dict((await s.execute(select(UserActivity.day, func.count(func.distinct(UserActivity.user_id)))
                                   .where(UserActivity.day >= start).group_by(UserActivity.day))).all())
    regs = (await s.scalars(select(reg).where(reg >= _day_start(start)))).all()
    new_by: dict[date, int] = {}
    for r in regs:
        if r:
            new_by[r.date()] = new_by.get(r.date(), 0) + 1
    refs = (await s.scalars(select(Referral.created_at).where(
        Referral.valid.is_(True), Referral.created_at >= _day_start(start)))).all()
    ref_by: dict[date, int] = {}
    for r in refs:
        ref_by[r.date()] = ref_by.get(r.date(), 0) + 1

    def key(d):
        return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    active = {key(k): v for k, v in active.items()}
    data["trend"] = [{"day": (start + timedelta(days=i)).isoformat(),
                      "active": int(active.get(start + timedelta(days=i), 0)),
                      "new": new_by.get(start + timedelta(days=i), 0),
                      "referrals": ref_by.get(start + timedelta(days=i), 0)} for i in range(30)]
    data["top_referrers"] = await leaderboard(s, 10, include_ids=True)
    return data


def user_filter(q: str = "", status: str = ""):
    w = []
    if status in (ACTIVE, SUSPENDED):
        w.append(User.status == status if status == SUSPENDED else or_(User.status.is_(None), User.status == ACTIVE))
    q = (q or "").strip().lstrip("@")
    if q:
        like = f"%{q.lower()}%"
        conds = [func.lower(User.username).like(like), func.lower(User.first_name).like(like)]
        if q.isdigit():
            conds.append(User.tg_id == int(q))
        w.append(or_(*conds))
    return and_(*w) if w else None


async def admin_user_rows(s: AsyncSession, q: str = "", status: str = "", offset: int = 0,
                          limit: int = 25) -> tuple[int, list[dict]]:
    cond = user_filter(q, status)
    base = select(User)
    count_q = select(func.count(User.tg_id))
    if cond is not None:
        base, count_q = base.where(cond), count_q.where(cond)
    total = await s.scalar(count_q) or 0
    users = (await s.scalars(base.order_by(func.coalesce(User.registered_at, User.created_at).desc(),
                                          User.tg_id.desc()).offset(offset).limit(limit))).all()
    ids = [u.tg_id for u in users]
    refs = dict((await s.execute(select(Referral.referrer_id, func.count(Referral.id)).where(
        Referral.referrer_id.in_(ids), Referral.valid.is_(True)).group_by(Referral.referrer_id))).all()) if ids else {}
    referrer_ids = {u.referred_by for u in users if u.referred_by}
    referrers = {u.tg_id: u for u in (await s.scalars(select(User).where(User.tg_id.in_(referrer_ids)))).all()} \
        if referrer_ids else {}
    admins = get_settings().admin_ids
    rows = []
    for u in users:
        src = u.referral_source or "legacy"
        if u.referred_by:
            src = f"referral by {display_name(referrers.get(u.referred_by))} ({u.referred_by})"
        rows.append({"id": u.tg_id, "username": u.username or "", "first_name": u.first_name or "",
                     "registered_at": iso(u.registered_at or u.created_at), "last_active_at": iso(u.last_active_at),
                     "referral_source": src, "successful_referrals": int(refs.get(u.tg_id, 0)),
                     "status": u.status or ACTIVE, "is_admin": u.tg_id in admins,
                     "opened_app": u.opened_app_at is not None})
    return total, rows
