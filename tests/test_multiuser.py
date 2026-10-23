"""Multi-user upgrade: auth/upsert, referrals, admin, suspension, isolation, limits, migration, security."""
import csv
import hashlib
import hmac
import io
import json
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import accounts as acc
from app import auth
from app.db import Referral, SessionLocal, User, UserActivity, utcnow

TOKEN = "123456:TEST-token-not-real"
ADMIN, ADMIN2 = 999, 1000          # ADMIN_IDS and ADMIN_TELEGRAM_IDS (conftest) -> union
_next = [700_000]


def uid():
    _next[0] += 1
    return _next[0]


def idata(user_id, username="u", first_name="User", start_param=None, is_bot=False, token=TOKEN,
          auth_date=None, extra_user=None):
    user = {"id": user_id, "first_name": first_name, "username": username}
    if is_bot:
        user["is_bot"] = True
    user.update(extra_user or {})
    f = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH", "user": json.dumps(user)}
    if start_param:
        f["start_param"] = start_param
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    f["hash"] = hmac.new(sec, dcs.encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode
    return urlencode(f)


def H(user_id, **kw):
    return {"X-Telegram-Init-Data": idata(user_id, **kw)}


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


# ------------------------------------------------------------------ auth
def test_initdata_valid_invalid_expired_future_and_bot(client):
    u = uid()
    assert client.get("/api/me", headers=H(u)).status_code == 200
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=H(u, token="999:other")).status_code == 401
    assert client.get("/api/me", headers=H(u, auth_date=int(time.time()) - 200_000)).status_code == 401
    assert client.get("/api/me", headers=H(u, auth_date=int(time.time()) + 3600)).status_code == 401
    tampered = idata(u).replace(f"%22id%22%3A+{u}", f"%22id%22%3A+{ADMIN}")
    assert client.get("/api/me", headers={"X-Telegram-Init-Data": tampered}).status_code == 401
    assert client.get("/api/me", headers=H(uid(), is_bot=True)).status_code == 403


def test_user_upsert_and_throttled_activity(client):
    u = uid()
    me = client.get("/api/me", headers=H(u, username="alice", first_name="Alice")).json()
    assert me["connected"] is True and me["id"] == u and me["username"] == "alice" and not me["is_admin"]
    assert me["referral"]["code"] and me["referral"]["bot_link"] == f"https://t.me/Test_jobs_bot?start=ref_{me['referral']['code']}"
    row = db(client, lambda s: s.get(User, u))
    assert row.registered_at and row.last_active_at and row.opened_app_at and row.status == "active"
    assert row.referral_source == "miniapp"
    first_active = row.last_active_at
    client.get("/api/me", headers=H(u, username="alice2", first_name="Alice"))
    row = db(client, lambda s: s.get(User, u))
    assert row.username == "alice2"          # profile changes are saved
    client.get("/api/jobs", headers=H(u, username="alice2", first_name="Alice"))
    row2 = db(client, lambda s: s.get(User, u))
    assert row2.last_active_at == row.last_active_at   # no write within the throttle window
    assert row.last_active_at >= first_active

    async def acts(s):
        from sqlalchemy import select
        return (await s.scalars(select(UserActivity).where(UserActivity.user_id == u))).all()
    assert len(db(client, acts)) == 1          # one row per user/day/kind


def test_client_cannot_claim_admin(client):
    u = uid()
    # extra fields in the signed user object or query params never grant admin
    r = client.get("/api/me?is_admin=true&user_id=999", headers=H(u, extra_user={"is_admin": True}))
    assert r.json()["is_admin"] is False and r.json()["id"] == u
    assert client.get("/api/admin/stats", headers=H(u, extra_user={"is_admin": True})).status_code == 403


# ------------------------------------------------------------------ referrals
def _code(client, user_id, **kw):
    return client.get("/api/me", headers=H(user_id, **kw)).json()["referral"]["code"]


def test_referral_attribution_via_miniapp_start_param(client):
    a, b = uid(), uid()
    code = _code(client, a, username="ref_a")
    r = client.get("/api/me", headers=H(b, start_param=f"ref_{code}"))
    assert r.status_code == 200 and r.json()["referred"] is True
    me_a = client.get("/api/me", headers=H(a, username="ref_a")).json()["referral"]
    assert me_a["invited"] == 1 and me_a["valid"] == 1 and me_a["points"] == 10
    ref = db(client, lambda s: s.scalar(__import__("sqlalchemy").select(Referral).where(Referral.referred_id == b)))
    assert ref.referrer_id == a and ref.valid and ref.channel == "miniapp"


def test_self_referral_and_duplicate_and_existing_user(client):
    a, c, d = uid(), uid(), uid()
    code_a, code_c = _code(client, a), _code(client, c)

    async def self_ref(s):
        ua = await s.get(User, a)
        return await acc.attribute(s, ua, code_a, "miniapp")
    assert db(client, self_ref) == "self_referral"

    # new user d via a, then a second attribution attempt (c) is refused by the DB unique key
    client.get("/api/me", headers=H(d, start_param=f"ref_{code_a}"))

    async def dup(s):
        ud = await s.get(User, d)
        return await acc.attribute(s, ud, code_c, "bot")
    assert db(client, dup) == "already_referred"
    assert db(client, lambda s: s.get(User, d)).referred_by == a

    # an existing user opening someone's link later is never re-attributed
    e = uid()
    client.get("/api/me", headers=H(e))
    client.get("/api/me", headers=H(e, start_param=f"ref_{code_c}"))
    assert db(client, lambda s: s.get(User, e)).referred_by is None
    assert client.get("/api/me", headers=H(c)).json()["referral"]["invited"] == 0


def test_suspended_referrer_ignored_and_hourly_cap(client, monkeypatch):
    a, b, c, d = uid(), uid(), uid(), uid()
    code = _code(client, a)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "referral_hourly_cap", 1)
    client.get("/api/me", headers=H(b, start_param=f"ref_{code}"))
    client.get("/api/me", headers=H(c, start_param=f"ref_{code}"))
    stats = client.get("/api/me", headers=H(a)).json()["referral"]
    assert stats["invited"] == 2 and stats["valid"] == 1 and stats["points"] == 10   # 2nd over cap: invalid
    assert client.post(f"/api/admin/users/{a}/status", json={"status": "suspended"}, headers=H(ADMIN)).json()["ok"]
    client.get("/api/me", headers=H(d, start_param=f"ref_{code}"))
    assert db(client, lambda s: s.get(User, d)).referred_by is None
    client.post(f"/api/admin/users/{a}/status", json={"status": "active"}, headers=H(ADMIN))


@pytest.mark.asyncio
async def test_bot_start_deep_link_attributes_new_user():
    from aiogram.filters import CommandObject
    from app.bot import handlers
    from app.db import init_db
    await init_db()
    a, b = uid(), uid()
    async with SessionLocal() as s:
        ua, _, _ = await acc.upsert_user(s, {"id": a, "first_name": "A"}, "bot")
        code = ua.referral_code
        await s.commit()
    m = MagicMock()
    m.from_user = MagicMock(id=b, username="newbie", first_name="New", is_bot=False)
    m.answer = AsyncMock()
    await handlers.cmd_start(m, AsyncMock(), CommandObject(prefix="/", command="start", args=f"ref_{code}"))
    text = m.answer.call_args.args[0]
    assert "friend's invite" in text and "Open Mini App" in text and "start=ref_" in text
    markup = m.answer.call_args.kwargs["reply_markup"]
    assert markup is not None
    async with SessionLocal() as s:
        assert (await s.get(User, b)).referred_by == a
        assert (await acc.referral_stats(s, a))["valid"] == 1
    # a bot account sending /start is ignored
    m2 = MagicMock()
    m2.from_user = MagicMock(id=uid(), is_bot=True)
    m2.answer = AsyncMock()
    await handlers.cmd_start(m2, AsyncMock(), None)
    m2.answer.assert_not_called()


def test_leaderboard_hides_ids_for_non_admins(client):
    a, b = uid(), uid()
    code = _code(client, a, username="leader_x")
    client.get("/api/me", headers=H(b, start_param=f"ref_{code}"))
    lb = client.get("/api/referrals/leaderboard", headers=H(b)).json()["leaderboard"]
    assert lb and all("id" not in r for r in lb)
    assert any(r["name"] == "@leader_x" for r in lb)
    lb_admin = client.get("/api/referrals/leaderboard", headers=H(ADMIN)).json()["leaderboard"]
    assert all("id" in r for r in lb_admin)


def test_referral_links_with_miniapp_short_name(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "miniapp_short_name", "jobs")
    links = acc.referral_links("abc23456")
    assert links["app_link"] == "https://t.me/Test_jobs_bot/jobs?startapp=ref_abc23456"
    assert links["share_url"].startswith("https://t.me/share/url?url=https%3A%2F%2Ft.me%2FTest_jobs_bot%2Fjobs")
    assert acc.parse_ref("ref_ABC23456") == "abc23456" and acc.parse_ref("ref_<script>") is None
    assert acc.parse_ref("hello") is None


# ------------------------------------------------------------------ admin
ADMIN_GETS = ["/api/admin/stats", "/api/admin/users", "/api/admin/users.csv", "/api/admin/audit",
              "/api/admin/referral-config"]


def test_admin_endpoints_forbidden_for_non_admin_and_forged(client):
    u = uid()
    for p in ADMIN_GETS:
        assert client.get(p, headers=H(u)).status_code == 403, p
        assert client.get(p).status_code == 401, p
        # forged: claims an admin id but signed with the wrong key
        assert client.get(p, headers=H(ADMIN, token="111:forged")).status_code == 401, p
    assert client.post(f"/api/admin/users/{u}/status", json={"status": "suspended"}, headers=H(u)).status_code == 403
    assert client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": 5},
                      headers=H(u)).status_code == 403
    for admin in (ADMIN, ADMIN2):     # both env vars are honoured
        assert client.get("/api/admin/stats", headers=H(admin)).status_code == 200
    assert client.get("/api/me", headers=H(ADMIN2)).json()["is_admin"] is True


def test_admin_cannot_suspend_self_or_admin(client):
    client.get("/api/me", headers=H(ADMIN2))
    assert client.post(f"/api/admin/users/{ADMIN}/status", json={"status": "suspended"}, headers=H(ADMIN)).status_code == 400
    assert client.post(f"/api/admin/users/{ADMIN2}/status", json={"status": "suspended"}, headers=H(ADMIN)).status_code == 400
    assert client.post("/api/admin/users/123/status", json={"status": "banned"}, headers=H(ADMIN)).status_code == 422


def test_suspended_user_blocked_and_audited(client):
    u = uid()
    client.get("/api/me", headers=H(u))
    r = client.post(f"/api/admin/users/{u}/status", json={"status": "suspended", "reason": "spam"}, headers=H(ADMIN))
    assert r.json()["status"] == "suspended"
    for p in ["/api/me", "/api/jobs", "/api/saved", "/api/profile", "/api/agent/leads", "/api/agent/dashboard"]:
        res = client.get(p, headers=H(u))
        assert res.status_code == 403 and res.json()["detail"] == "account_suspended", p
    audit = client.get("/api/admin/audit", headers=H(ADMIN)).json()["entries"]
    e = next(x for x in audit if x["target"] == str(u))
    assert e["action"] == "suspend_user" and e["admin_id"] == ADMIN and "spam" in e["details"]
    rows = client.get(f"/api/admin/users?status=suspended&q={u}", headers=H(ADMIN)).json()
    assert rows["total"] == 1 and rows["users"][0]["status"] == "suspended"
    client.post(f"/api/admin/users/{u}/status", json={"status": "active"}, headers=H(ADMIN))
    assert client.get("/api/jobs", headers=H(u)).status_code == 200
    assert client.get("/api/admin/audit", headers=H(ADMIN)).json()["entries"][0]["action"] == "reactivate_user"


def test_admin_stats_numbers(client):
    before = client.get("/api/admin/stats", headers=H(ADMIN)).json()
    a, b, c = uid(), uid(), uid()
    code = _code(client, a)
    client.get("/api/me", headers=H(b, start_param=f"ref_{code}"))

    async def bot_only(s):          # a user who only used the bot (never opened the Mini App)
        await acc.upsert_user(s, {"id": c, "first_name": "Bot user"}, "bot")
    db(client, bot_only)
    after = client.get("/api/admin/stats", headers=H(ADMIN)).json()
    assert after["total_users"] - before["total_users"] == 3
    assert after["opened_app"] - before["opened_app"] == 2
    assert after["new_today"] - before["new_today"] == 3
    assert after["dau"] - before["dau"] == 3
    assert after["wau"] >= after["dau"] and after["mau"] >= after["wau"]
    assert after["referral_registrations"] - before["referral_registrations"] == 1
    assert after["referral_links"] - before["referral_links"] == 3
    assert len(after["trend"]) == 30 and after["trend"][-1]["day"] == utcnow().date().isoformat()
    assert after["trend"][-1]["new"] - before["trend"][-1]["new"] == 3
    assert after["trend"][-1]["active"] == after["dau"]
    assert all("id" in r for r in after["top_referrers"])


def test_users_list_search_pagination_and_csv(client):
    u = uid()
    client.get("/api/me", headers=H(u, username="csvtester", first_name="=HYPERLINK(\"http://evil\")"))
    page = client.get("/api/admin/users?limit=2&offset=0", headers=H(ADMIN)).json()
    assert len(page["users"]) == 2 and page["total"] >= 2
    found = client.get("/api/admin/users?q=csvtest", headers=H(ADMIN)).json()
    assert found["total"] == 1 and found["users"][0]["id"] == u
    r = client.get("/api/admin/users.csv?q=csvtest", headers=H(ADMIN))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["id", "username", "first_name"]
    assert rows[1][0] == str(u) and rows[1][2].startswith("'=HYPERLINK")
    audit = client.get("/api/admin/audit", headers=H(ADMIN)).json()["entries"]
    assert audit[0]["action"] == "export_users_csv"


def test_referral_config_editable_and_audited(client):
    r = client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": 25}, headers=H(ADMIN))
    assert r.json()["points_per_referral"] == 25 and "no cash" in r.json()["label"]
    assert client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": -1},
                      headers=H(ADMIN)).status_code == 422
    a, b = uid(), uid()
    code = _code(client, a)
    client.get("/api/me", headers=H(b, start_param=f"ref_{code}"))
    assert client.get("/api/me", headers=H(a)).json()["referral"]["points"] == 25
    client.put("/api/admin/referral-config", json={"enabled": False, "points_per_referral": 25}, headers=H(ADMIN))
    c = uid()
    client.get("/api/me", headers=H(c, start_param=f"ref_{code}"))
    me = client.get("/api/me", headers=H(a)).json()["referral"]
    assert me["valid"] == 2 and me["points"] == 25 and me["points_enabled"] is False
    assert client.get("/api/admin/audit", headers=H(ADMIN)).json()["entries"][0]["action"] == "update_referral_config"
    client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": 10}, headers=H(ADMIN))


# ------------------------------------------------------------------ per-user data isolation
def _seed_lead(client, owner):
    from app.outreach.models import Contact, Draft, Lead

    async def run(s):
        lead = Lead(owner_id=owner, uid=f"iso-{owner}", source="x", source_url="https://x.com/p/status/1",
                    project="IsoChain", title="Moderator", role="moderator", relevance=80, raw_text="hiring mod")
        s.add(lead)
        await s.flush()
        s.add(Contact(lead_id=lead.id, platform="telegram", handle=f"hr_{owner}", role_hint="post author"))
        d = Draft(owner_id=owner, lead_id=lead.id, channel="telegram", recipient=f"hr_{owner}", text="hi")
        s.add(d)
        await s.flush()
        return lead.id, d.id
    return db(client, run)


def test_per_user_isolation_leads_drafts_saved(client):
    x, y = uid(), uid()
    client.get("/api/me", headers=H(x))
    client.get("/api/me", headers=H(y))
    lead_id, draft_id = _seed_lead(client, x)
    assert client.get("/api/agent/leads", headers=H(x)).json()["total"] == 1
    assert client.get("/api/agent/leads", headers=H(y)).json()["total"] == 0
    assert client.get(f"/api/agent/leads/{lead_id}", headers=H(y)).status_code == 404
    assert client.patch(f"/api/agent/leads/{lead_id}", json={"status": "saved"}, headers=H(y)).status_code == 404
    assert client.post(f"/api/agent/leads/{lead_id}/draft", json={}, headers=H(y)).status_code == 404
    assert client.post(f"/api/agent/drafts/{draft_id}", json={"action": "skip"}, headers=H(y)).status_code == 404
    assert client.get("/api/agent/drafts?status=pending", headers=H(y)).json()["drafts"] == []
    assert client.get("/api/agent/contacts", headers=H(y)).json()["contacts"] == []
    assert len(client.get("/api/agent/drafts?status=pending", headers=H(x)).json()["drafts"]) == 1

    from app.db import Job

    async def job(s):
        j = Job(uid=f"iso:{x}", source="t", source_name="T", source_type="company_ats", title="Mod",
                company="C", url="https://e.x/1", apply_url="https://e.x/1")
        s.add(j)
        await s.flush()
        return j.id
    jid = db(client, job)
    client.post(f"/api/saved/{jid}", headers=H(x))
    assert jid in [j["id"] for j in client.get("/api/saved", headers=H(x)).json()["jobs"]]
    assert jid not in [j["id"] for j in client.get("/api/saved", headers=H(y)).json()["jobs"]]


def test_normal_user_cannot_send_from_personal_account(client):
    x = uid()
    client.get("/api/me", headers=H(x))
    _, draft_id = _seed_lead(client, x)
    r = client.post(f"/api/agent/drafts/{draft_id}", json={"action": "approve"}, headers=H(x))
    assert r.status_code == 403 and "admin-only" in r.json()["detail"]
    assert client.post(f"/api/agent/drafts/{draft_id}", json={"action": "mark_sent"}, headers=H(x)).json()["ok"]
    from app.outreach import agent as A
    assert client.portal.call(A.try_send, x, draft_id)["code"] in ("admin_only", "not_approved")
    assert client.post("/api/agent/search", json={"sources": ["telegram"]}, headers=H(x)).status_code == 403
    client.put("/api/agent/settings", json={"auto_search_hours": 6}, headers=H(x))
    assert client.get("/api/agent/settings", headers=H(x)).json()["auto_search_hours"] == 0


# ------------------------------------------------------------------ daily limits
def test_daily_limits_for_users_admin_unlimited(client, monkeypatch):
    from app.config import get_settings
    from app.outreach import agent as A
    monkeypatch.setattr(get_settings(), "user_daily_searches", 1)
    monkeypatch.setattr(get_settings(), "user_daily_drafts", 1)
    calls = []
    monkeypatch.setattr(A, "start_discovery", lambda owner, sources, q="": calls.append((owner, sources)) or True)
    x = uid()
    client.get("/api/me", headers=H(x))
    assert client.post("/api/agent/search", json={"sources": ["google", "telegram"]}, headers=H(x)).status_code == 200
    assert calls[-1] == (x, ["google"])                     # admin-only source dropped
    r = client.post("/api/agent/search", json={"sources": ["google"]}, headers=H(x))
    assert r.status_code == 429 and r.json()["code"] == "daily_limit"
    lim = client.get("/api/me", headers=H(x)).json()["limits"]
    assert lim["search"] == {"limit": 1, "used": 1, "left": 0}

    lead_id, _ = _seed_lead(client, x)
    assert client.post(f"/api/agent/leads/{lead_id}/draft", json={}, headers=H(x)).status_code == 200
    r = client.post(f"/api/agent/leads/{lead_id}/draft", json={}, headers=H(x))
    assert r.status_code == 429 and r.json()["kind"] == "draft"

    for _ in range(3):                                      # admins are unlimited
        assert client.post("/api/agent/search", json={"sources": ["google"]}, headers=H(ADMIN2)).status_code == 200
    assert client.get("/api/me", headers=H(ADMIN2)).json()["limits"] == {"unlimited": True}


# ------------------------------------------------------------------ security helpers
def test_rate_limit_per_user(client):
    u = uid()
    auth.user_bucket.configure(3, burst=3)
    try:
        codes = [client.get("/api/me", headers=H(u)).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[-1] == 429
    finally:
        auth.user_bucket.configure(100000)


def test_rate_limit_per_ip(client):
    auth.ip_bucket.configure(2, burst=2)
    try:
        codes = [client.get("/api/botinfo").status_code for _ in range(4)]
        assert codes[-1] == 429
    finally:
        auth.ip_bucket.configure(1000000)


def test_csv_safe():
    assert acc.csv_safe("=1+2") == "'=1+2"
    assert acc.csv_safe("+cmd") == "'+cmd" and acc.csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert acc.csv_safe("-2") == "'-2" and acc.csv_safe("normal") == "normal" and acc.csv_safe(None) == ""


def test_no_secrets_or_hardcoded_bot_username_in_frontend():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for p in (root / "webapp").glob("*"):
        t = p.read_text(errors="ignore")
        assert "BOT_TOKEN" not in t and "Joblist_aibot" not in t, p
    for p in (root / "app").rglob("*.py"):
        assert "Joblist_aibot" not in p.read_text(errors="ignore"), p


# ------------------------------------------------------------------ migration
@pytest.mark.asyncio
async def test_migration_adds_columns_without_losing_data(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.db import Base, migrate
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with eng.begin() as conn:      # the pre-upgrade production schema
        await conn.execute(text("CREATE TABLE users (tg_id BIGINT PRIMARY KEY, username VARCHAR(64), "
                                "first_name VARCHAR(128), roles TEXT, skills TEXT, remote_only BOOLEAN, "
                                "alerts_enabled BOOLEAN, created_at DATETIME)"))
        await conn.execute(text("INSERT INTO users VALUES (5, 'old', 'Old', 'community', 'discord', 0, 1, "
                                "'2026-10-01 10:00:00')"))
    for _ in range(2):                   # idempotent
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await migrate(conn)
    async with eng.begin() as conn:
        row = (await conn.execute(text("SELECT tg_id, username, roles, alerts_enabled, registered_at, status, "
                                       "referral_code, referral_source FROM users"))).one()
        tables = {r[0] for r in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()}
    assert row[0] == 5 and row[1] == "old" and row[2] == "community" and row[3] == 1
    assert str(row[4]).startswith("2026-10-01") and row[5] == "active" and row[6] is None and row[7] == "legacy"
    assert {"referrals", "referral_events", "user_activity", "admin_audit_log", "app_settings",
            "usage_counters"} <= tables
    await eng.dispose()


def test_postgres_migration_sql_is_idempotent():
    from app.db import migration_statements
    stmts = migration_statements("postgresql", {"users": {"tg_id", "username"}})
    alters = [x for x in stmts if x.startswith("ALTER")]
    assert alters and all("ADD COLUMN IF NOT EXISTS" in x for x in alters)
    assert not any("DROP" in x.upper() for x in stmts)
    assert any("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code" in x for x in stmts)
    # fresh database (table created by create_all): no ALTERs needed
    assert not [x for x in migration_statements("postgresql", {}) if x.startswith("ALTER")]
