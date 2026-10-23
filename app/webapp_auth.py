"""Telegram Mini App initData validation.

Implements https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  hash       = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
The frontend sends window.Telegram.WebApp.initData in the "X-Telegram-Init-Data" header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from .config import get_settings


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    if not init_data or not bot_token:
        raise InitDataError("missing init data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("missing hash")
    # Note: every other field (including "signature", if present) is part of the check string.
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad hash")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("bad auth_date")
    if max_age and time.time() - auth_date > max_age:
        raise InitDataError("init data expired")
    user = json.loads(pairs.get("user", "{}") or "{}")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("no user")
    return user


async def telegram_user(x_telegram_init_data: str = Header(default="")) -> dict:
    """FastAPI dependency: returns the verified Telegram user or raises 401."""
    st = get_settings()
    try:
        return validate_init_data(x_telegram_init_data, st.bot_token, st.initdata_max_age)
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=f"Telegram auth failed: {e}")
