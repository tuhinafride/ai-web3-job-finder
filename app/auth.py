"""FastAPI auth dependencies for the Mini App API.

Every protected route depends on `current_user`, which:
  1. verifies Telegram initData (HMAC with the bot token + auth_date max age),
  2. rate-limits per Telegram user,
  3. upserts the user row (throttled writes) and handles first-seen referral attribution,
  4. rejects suspended users with 403.
`is_admin` is computed on the server from ADMIN_IDS / ADMIN_TELEGRAM_IDS only.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from . import accounts as acc
from .config import get_settings
from .db import SessionLocal
from .ratelimit import TokenBucket
from .webapp_auth import InitDataError, validate_init_data_full

_s = get_settings()
user_bucket = TokenBucket(_s.rate_limit_user_per_min, burst=max(10, _s.rate_limit_user_per_min))
ip_bucket = TokenBucket(_s.rate_limit_ip_per_min, burst=max(20, _s.rate_limit_ip_per_min))


async def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    st = get_settings()
    try:
        data = validate_init_data_full(x_telegram_init_data, st.bot_token, st.initdata_max_age)
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=f"Telegram auth failed: {e}")
    tg = data["user"]
    if tg.get("is_bot"):
        raise HTTPException(status_code=403, detail="bot_accounts_not_allowed")
    if not user_bucket.allow(f"u:{tg['id']}"):
        raise HTTPException(status_code=429, detail="Too many requests. Slow down a little.")
    async with SessionLocal() as s:
        u, is_new, outcome = await acc.upsert_user(s, tg, "miniapp", data["start_param"])
        await s.commit()
    if u.status == acc.SUSPENDED:
        raise HTTPException(status_code=403, detail="account_suspended")
    return {"id": u.tg_id, "username": u.username, "first_name": u.first_name,
            "is_admin": acc.is_admin_id(u.tg_id), "status": u.status or acc.ACTIVE,
            "is_new": is_new, "referral_outcome": outcome}


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="admin_only")
    return user
