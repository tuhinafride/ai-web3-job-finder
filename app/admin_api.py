"""Admin API (/api/admin/*). Every route re-verifies initData and the admin id on each request."""
from __future__ import annotations

import csv
import io
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import accounts as acc
from .auth import admin_user
from .db import AdminAuditLog, SessionLocal, User

router = APIRouter(prefix="/api/admin")


@router.get("/stats")
async def stats(admin: dict = Depends(admin_user)):
    async with SessionLocal() as s:
        return await acc.admin_stats(s)


@router.get("/users")
async def users(admin: dict = Depends(admin_user), q: str = Query("", max_length=64),
                status: Literal["", "active", "suspended"] = "",
                offset: int = Query(0, ge=0, le=1_000_000), limit: int = Query(25, ge=1, le=100)):
    async with SessionLocal() as s:
        total, rows = await acc.admin_user_rows(s, q, status, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "users": rows}


CSV_COLUMNS = ["id", "username", "first_name", "registered_at", "last_active_at", "referral_source",
               "successful_referrals", "status", "opened_app"]


@router.get("/users.csv")
async def users_csv(admin: dict = Depends(admin_user), q: str = Query("", max_length=64),
                    status: Literal["", "active", "suspended"] = ""):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    async with SessionLocal() as s:
        offset = 0
        while True:
            _, rows = await acc.admin_user_rows(s, q, status, offset, 500)
            for r in rows:
                w.writerow([acc.csv_safe(r[c]) for c in CSV_COLUMNS])
            if len(rows) < 500:
                break
            offset += 500
        await acc.audit(s, admin["id"], "export_users_csv", "users", json.dumps({"q": q, "status": status}))
        await s.commit()
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="users.csv"'})


class StatusIn(BaseModel):
    status: Literal["active", "suspended"]
    reason: str = Field("", max_length=300)


@router.post("/users/{user_id}/status")
async def set_status(user_id: int, body: StatusIn, admin: dict = Depends(admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't change your own status.")
    if acc.is_admin_id(user_id):
        raise HTTPException(400, "Admins can't be suspended.")
    async with SessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            raise HTTPException(404, "User not found")
        old = u.status or acc.ACTIVE
        u.status = body.status
        await acc.audit(s, admin["id"], "suspend_user" if body.status == "suspended" else "reactivate_user",
                        str(user_id), json.dumps({"from": old, "to": body.status, "reason": body.reason.strip()}))
        await s.commit()
    return {"ok": True, "id": user_id, "status": body.status}


@router.get("/audit")
async def audit_log(admin: dict = Depends(admin_user), limit: int = Query(50, ge=1, le=200)):
    async with SessionLocal() as s:
        rows = (await s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(limit))).all()
    return {"entries": [{"id": r.id, "admin_id": r.admin_id, "action": r.action, "target": r.target,
                         "details": r.details, "created_at": acc.iso(r.created_at)} for r in rows]}


class ReferralConfigIn(BaseModel):
    enabled: bool
    points_per_referral: int = Field(ge=0, le=1000)


@router.get("/referral-config")
async def get_ref_config(admin: dict = Depends(admin_user)):
    async with SessionLocal() as s:
        return await acc.referral_config(s)


@router.put("/referral-config")
async def put_ref_config(body: ReferralConfigIn, admin: dict = Depends(admin_user)):
    async with SessionLocal() as s:
        before = await acc.referral_config(s)
        await acc.set_setting(s, "referral_enabled", "true" if body.enabled else "false")
        await acc.set_setting(s, "referral_points", str(body.points_per_referral))
        await acc.audit(s, admin["id"], "update_referral_config", "app_settings",
                        json.dumps({"from": {k: before[k] for k in ("enabled", "points_per_referral")},
                                    "to": body.model_dump()}))
        await s.commit()
        return await acc.referral_config(s)
