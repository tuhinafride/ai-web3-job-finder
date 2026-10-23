"""Presence counter for the Mini App Home screen.

GET  /api/presence                  any authenticated, non-suspended user
GET  /api/admin/presence-settings   admin only
PUT  /api/admin/presence-settings   admin only, audit-logged

Settings live in app_settings under the key "presence_counter" as JSON:
  {"mode": "custom" | "real", "display": "25,000+"}
Default is mode "custom" with display "25,000+" (owner decision). In custom mode non-admins only get the
label; admins always get the real numbers too.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from . import accounts as acc
from .auth import admin_user, current_user
from .db import SessionLocal, User, utcnow

router = APIRouter()

SETTING_KEY = "presence_counter"
ONLINE_WINDOW = timedelta(minutes=5)
DEFAULT_CONFIG = {"mode": "custom", "display": "25,000+"}


def _clean_display(v: str) -> str:
    v = " ".join((v or "").split())
    if len(v) > 24:
        raise ValueError("display must be at most 24 characters")
    if any(c in v for c in "<>\"'`\\"):
        raise ValueError("display contains characters that aren't allowed")
    return v


async def get_config(s) -> dict:
    raw = await acc.get_setting(s, SETTING_KEY)
    cfg = dict(DEFAULT_CONFIG)
    if raw:
        try:
            data = json.loads(raw)
            if data.get("mode") in ("real", "custom"):
                cfg["mode"] = data["mode"]
            if isinstance(data.get("display"), str):
                cfg["display"] = _clean_display(data["display"]) or DEFAULT_CONFIG["display"]
        except (ValueError, TypeError, AttributeError):
            pass
    return cfg


async def real_counts(s, now: datetime | None = None) -> tuple[int, int]:
    now = now or utcnow()
    not_suspended = or_(User.status.is_(None), User.status != acc.SUSPENDED)
    online = await s.scalar(select(func.count(func.distinct(User.tg_id)))
                            .where(User.last_active_at >= now - ONLINE_WINDOW, not_suspended)) or 0
    day_start = datetime(now.year, now.month, now.day)
    today = await s.scalar(select(func.count(func.distinct(User.tg_id)))
                           .where(User.last_active_at >= day_start, not_suspended)) or 0
    return int(online), int(today)


@router.get("/api/presence")
async def presence(user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        cfg = await get_config(s)
        online, today = await real_counts(s)
    if cfg["mode"] == "real":
        return {"online_now": online, "active_today": today, "mode": "real", "display": f"{online:,}"}
    out = {"online_now": None, "active_today": None, "mode": "custom", "display": cfg["display"]}
    if user["is_admin"]:
        out.update(online_now=online, active_today=today)
    return out


class PresenceSettingsIn(BaseModel):
    mode: Literal["real", "custom"]
    display: str = Field("", max_length=64)

    @field_validator("display")
    @classmethod
    def _v(cls, v: str) -> str:
        return _clean_display(v)


@router.get("/api/admin/presence-settings")
async def get_presence_settings(admin: dict = Depends(admin_user)):
    async with SessionLocal() as s:
        cfg = await get_config(s)
        online, today = await real_counts(s)
    return {**cfg, "online_now": online, "active_today": today}


@router.put("/api/admin/presence-settings")
async def put_presence_settings(body: PresenceSettingsIn, admin: dict = Depends(admin_user)):
    if body.mode == "custom" and not body.display:
        raise HTTPException(422, "display is required when mode is custom")
    async with SessionLocal() as s:
        before = await get_config(s)
        new = {"mode": body.mode, "display": body.display or before["display"]}
        await acc.set_setting(s, SETTING_KEY, json.dumps(new))
        await acc.audit(s, admin["id"], "update_presence_counter", "app_settings",
                        json.dumps({"from": before, "to": new}))
        await s.commit()
        online, today = await real_counts(s)
    return {**new, "online_now": online, "active_today": today}
