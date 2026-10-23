"""/api/presence and /api/admin/presence-settings."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.db import AdminAuditLog, AppSetting, SessionLocal, User, utcnow
from tests.test_multiuser import ADMIN, H, uid


@pytest.fixture()
def client():
    from app import main
    with TestClient(main.app) as c:
        yield c


def db(client, fn):
    async def run():
        async with SessionLocal() as s:
            out = await fn(s)
            await s.commit()
            return out
    return client.portal.call(run)


@pytest.fixture()
def fresh(client):
    """Isolate: no presence setting, and everyone already in the DB marked long inactive."""
    async def reset(s):
        await s.execute(delete(AppSetting).where(AppSetting.key == "presence_counter"))
        await s.execute(update(User).values(last_active_at=utcnow() - timedelta(days=3)))
    db(client, reset)
    return client


def set_active(client, user_id, ago):
    async def f(s):
        await s.execute(update(User).where(User.tg_id == user_id).values(last_active_at=utcnow() - ago))
    db(client, f)


def test_presence_requires_auth(fresh):
    assert fresh.get("/api/presence").status_code == 401
    assert fresh.get("/api/presence", headers={"X-Telegram-Init-Data": "user=x&hash=bad"}).status_code == 401
    assert fresh.get("/api/admin/presence-settings").status_code == 401


def test_default_is_custom_label_for_everyone(fresh):
    u = uid()
    r = fresh.get("/api/presence", headers=H(u))
    assert r.status_code == 200
    d = r.json()
    assert d["mode"] == "custom" and d["display"] == "25,000+"
    assert d["online_now"] is None and d["active_today"] is None   # real number hidden from non-admins


def test_admin_gets_real_numbers_in_custom_mode(fresh):
    d = fresh.get("/api/presence", headers=H(ADMIN)).json()
    assert d["mode"] == "custom" and d["display"] == "25,000+"
    assert isinstance(d["online_now"], int) and d["online_now"] >= 1   # the admin themself just became active


def test_real_count_logic(fresh):
    a, b, c, susp = uid(), uid(), uid(), uid()
    for x in (a, b, c, susp):
        assert fresh.get("/api/me", headers=H(x)).status_code == 200
    set_active(fresh, a, timedelta(minutes=1))
    set_active(fresh, b, timedelta(minutes=4, seconds=50))
    set_active(fresh, c, timedelta(minutes=6))      # outside the 5-minute window
    async def suspend(s):
        await s.execute(update(User).where(User.tg_id == susp).values(status="suspended"))
    db(fresh, suspend)
    r = fresh.put("/api/admin/presence-settings", headers=H(ADMIN), json={"mode": "real", "display": ""})
    assert r.status_code == 200, r.text
    set_active(fresh, ADMIN, timedelta(days=3))     # take the admin out of the window again
    d = fresh.get("/api/presence", headers=H(a)).json()
    assert d["mode"] == "real"
    assert d["online_now"] == 2, d                  # a and b; c is 6 min old, susp is suspended
    assert d["display"] == "2"
    assert d["active_today"] >= 2


def test_suspended_user_cannot_read_presence(fresh):
    s_id = uid()
    fresh.get("/api/me", headers=H(s_id))
    async def suspend(s):
        await s.execute(update(User).where(User.tg_id == s_id).values(status="suspended"))
    db(fresh, suspend)
    assert fresh.get("/api/presence", headers=H(s_id)).status_code == 403


def test_settings_admin_only(fresh):
    u = uid()
    assert fresh.get("/api/admin/presence-settings", headers=H(u)).status_code == 403
    assert fresh.put("/api/admin/presence-settings", headers=H(u),
                     json={"mode": "real", "display": ""}).status_code == 403
    g = fresh.get("/api/admin/presence-settings", headers=H(ADMIN)).json()
    assert g["mode"] == "custom" and g["display"] == "25,000+" and isinstance(g["online_now"], int)


def test_custom_mode_update_validation_and_audit(fresh):
    r = fresh.put("/api/admin/presence-settings", headers=H(ADMIN), json={"mode": "custom", "display": "  10K+  "})
    assert r.status_code == 200 and r.json()["display"] == "10K+"
    assert fresh.get("/api/presence", headers=H(uid())).json()["display"] == "10K+"
    assert fresh.put("/api/admin/presence-settings", headers=H(ADMIN),
                     json={"mode": "custom", "display": ""}).status_code == 422
    assert fresh.put("/api/admin/presence-settings", headers=H(ADMIN),
                     json={"mode": "custom", "display": "<b>x</b>"}).status_code == 422
    assert fresh.put("/api/admin/presence-settings", headers=H(ADMIN),
                     json={"mode": "bogus", "display": "1"}).status_code == 422
    assert fresh.put("/api/admin/presence-settings", headers=H(ADMIN),
                     json={"mode": "custom", "display": "x" * 30}).status_code == 422

    async def last_audit(s):
        return (await s.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "update_presence_counter")
                                .order_by(AdminAuditLog.id.desc()))).first()
    row = db(fresh, last_audit)
    assert row is not None and row.admin_id == ADMIN and row.target == "app_settings"
    det = json.loads(row.details)
    assert det["to"] == {"mode": "custom", "display": "10K+"} and det["from"]["display"] == "25,000+"
    # also visible through the existing audit API
    entries = fresh.get("/api/admin/audit", headers=H(ADMIN)).json()["entries"]
    assert any(e["action"] == "update_presence_counter" for e in entries)


def test_corrupt_setting_falls_back_to_default(fresh):
    async def bad(s):
        s.add(AppSetting(key="presence_counter", value="{not json"))
    db(fresh, bad)
    d = fresh.get("/api/presence", headers=H(uid())).json()
    assert d["mode"] == "custom" and d["display"] == "25,000+"
