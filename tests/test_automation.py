"""Full automation mode: persistence/resume, pause/stop, app-open debounce, never-twice notifications,
digest, Telegram errors, reminder edit-vs-send, admin-only bot controls, API + settings round-trips."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.bot import automation_handlers as BH
from app.db import SessionLocal, init_db, utcnow
from app.outreach import agent as A
from app.outreach import automation as AU
from app.outreach import search as S
from app.outreach.models import AutomationState, Lead, LeadNotification
from tests.test_app import make_init_data

ADMIN, OTHER_ADMIN, USER = 1000, 999, 77


def h(uid=ADMIN):
    return {"X-Telegram-Init-Data": make_init_data(uid)}


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.pins = [], [], []
        self.edit_error = None
        self.send_error = None
        self._id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.send_error:
            raise self.send_error
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "kb": reply_markup, "id": self._id})
        return MagicMock(message_id=self._id)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, reply_markup=None, **kw):
        if self.edit_error:
            raise self.edit_error
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def pin_chat_message(self, chat_id=None, message_id=None, **kw):
        self.pins.append(message_id)


@pytest.fixture()
async def env(monkeypatch):
    await init_db()
    bot = FakeBot()
    AU.set_bot(bot)
    monkeypatch.setattr(AU, "PER_CHAT_GAP", 0)
    async with SessionLocal() as s:
        for uid in (ADMIN, OTHER_ADMIN):
            a = await s.get(AutomationState, uid)
            if a:
                await s.delete(a)
        await s.execute(LeadNotification.__table__.delete())
        st = await A.get_state(s, ADMIN)
        st.stopped = False
        await s.commit()
    S.backoff.clear()
    yield bot
    for t in list(AU._tasks.values()):
        if not t.done():
            t.cancel()
    AU._tasks.clear()
    AU.set_bot(None)


_n = {"i": 0}


async def make_lead(owner=ADMIN, relevance=80, scam="low", fp="", **kw):
    _n["i"] += 1
    async with SessionLocal() as s:
        l = Lead(owner_id=owner, uid=kw.pop("uid", f"auto-test-{_n['i']}"), fp=fp, source="google",
                 source_url=f"https://jobs.example/{_n['i']}", title=kw.pop("title", f"Discord Moderator {_n['i']}"),
                 project="NovaChain", role="moderator", relevance=relevance, scam_risk=scam,
                 scam_flags="Asks for an upfront fee" if scam != "low" else "", pay_status="paid",
                 pay_detail="$600/month", source_checked=True, **kw)
        s.add(l)
        await s.commit()
        return l.id


# ------------------------------------------------------------------ state persistence / resume
async def test_state_persists_and_resumes_after_restart(env):
    out = await AU.set_mode(ADMIN, "start")
    assert out["state"] == "running" and out["enabled"]
    async with SessionLocal() as s:                       # simulate a crash in the middle of a run
        a = await s.get(AutomationState, ADMIN)
        a.last_status, a.next_run_at = "running", None
        await s.commit()
    resumed = await AU.on_startup()
    assert ADMIN in resumed
    st = await AU.status(ADMIN)
    assert st["state"] == "running" and st["last_status"] == "interrupted" and st["next_run_at"]
    await AU.set_mode(ADMIN, "pause")
    assert ADMIN not in await AU.on_startup()             # paused stays paused after a restart
    assert (await AU.status(ADMIN))["state"] == "paused"


async def test_tick_runs_due_search_and_reminds(env, monkeypatch):
    lead_id = await make_lead()
    runs = []

    async def fake_discovery(owner_id, sources, custom_query="", trigger="manual"):
        runs.append((owner_id, sources, trigger))
        return {"new": 1, "errors": [], "new_ids": [lead_id], "by_source": {"google": 1}}
    monkeypatch.setattr(A, "run_discovery", fake_discovery)
    await AU.set_mode(ADMIN, "start")
    res = await AU.tick()
    assert ADMIN in res["started"] and ADMIN in res["reminded"]
    await AU._tasks[ADMIN]
    assert runs and runs[0][2] == "auto"
    st = await AU.status(ADMIN)
    assert st["last_status"] == "ok" and st["counters"]["last_new_leads"] == 1 and st["counters"]["leads_notified_total"] == 1
    assert st["next_run_at"] and st["counters"]["runs_total"] == 1
    lead_msgs = [m for m in env.sent if "Discord Moderator" in m["text"]]
    assert len(lead_msgs) == 1 and lead_msgs[0]["chat_id"] == ADMIN
    cbs = [b.callback_data for b in lead_msgs[0]["kb"].inline_keyboard[0]]
    assert cbs == [f"an|s|{lead_id}", f"an|d|{lead_id}", f"an|k|{lead_id}"]
    # not due yet -> nothing new is started
    res = await AU.tick()
    assert ADMIN not in res["started"]


async def test_pause_and_stop(env, monkeypatch):
    started = []

    async def spy(owner_id, trigger="manual"):
        started.append(owner_id)
        return True, "ok"
    await AU.set_mode(ADMIN, "start")
    await AU.set_mode(ADMIN, "pause")
    monkeypatch.setattr(AU, "trigger_run", spy)
    await AU.tick()
    assert started == []                                   # paused: no runs, no reminders
    assert env.sent == [] and env.edits == []
    out = await AU.set_mode(ADMIN, "stop")
    assert out["state"] == "stopped" and out["next_run_at"] is None
    await AU.tick()
    assert started == []


async def test_stop_cancels_running_search(env, monkeypatch):
    gate = asyncio.Event()

    async def slow(owner_id, sources, custom_query="", trigger="manual"):
        await gate.wait()
        return {"new_ids": [], "errors": []}
    monkeypatch.setattr(A, "run_discovery", slow)
    await AU.set_mode(ADMIN, "start")
    ok, _ = await AU.trigger_run(ADMIN, "manual")
    assert ok and AU.run_in_progress(ADMIN)
    assert (await AU.trigger_run(ADMIN, "manual"))[0] is False   # never two runs at once
    await AU.set_mode(ADMIN, "stop")
    await asyncio.sleep(0.05)
    assert not AU.run_in_progress(ADMIN)
    assert (await AU.status(ADMIN))["last_status"] == "cancelled"


async def test_run_skipped_when_agent_kill_switch_on(env, monkeypatch):
    await AU.set_mode(ADMIN, "start")
    await A.stop_agent(ADMIN, True)
    called = []

    async def fake(*a, **k):
        called.append(1)
    monkeypatch.setattr(A, "run_discovery", fake)
    await AU.run_once(ADMIN, "schedule")
    assert called == [] and (await AU.status(ADMIN))["last_status"] == "skipped"
    await A.stop_agent(ADMIN, False)


async def test_provider_failures_recorded_not_faked(env, monkeypatch):
    async def failing(owner_id, sources, custom_query="", trigger="manual"):
        return {"new": 0, "errors": ["Serper HTTP 403", "X API HTTP 401"], "new_ids": [], "by_source": {}}
    monkeypatch.setattr(A, "run_discovery", failing)
    await AU.set_mode(ADMIN, "start")
    res = await AU.run_once(ADMIN, "schedule")
    st = await AU.status(ADMIN)
    assert res["status"] == "error" and "Serper HTTP 403" in st["last_error"]
    assert st["counters"]["runs_failed"] == 1 and st["counters"]["leads_notified_total"] == 0
    assert not [m for m in env.sent if "🆕" in m["text"]]
    logs = await AU.logs(ADMIN)
    assert any("Serper HTTP 403" in l["message"] for l in logs)


async def test_run_with_crashing_provider_does_not_crash(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(A, "run_discovery", boom)
    await AU.set_mode(ADMIN, "start")
    res = await AU.run_once(ADMIN, "schedule")
    assert res["status"] == "error"
    assert "network down" in (await AU.status(ADMIN))["last_error"]


# ------------------------------------------------------------------ notifications: never twice
async def test_same_lead_never_notified_twice(env):
    a = await make_lead(fp="fp-dup-1")
    b = await make_lead(fp="fp-dup-1")                    # same text reposted under another URL
    n1, _ = await AU.notify_leads(ADMIN, [a, b], 50)
    n2, _ = await AU.notify_leads(ADMIN, [a, b], 50)
    assert n1 == 1 and n2 == 0
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count(LeadNotification.id)).where(LeadNotification.owner_id == ADMIN)) == 1
        s.add(LeadNotification(owner_id=ADMIN, lead_key=(await s.get(Lead, a)).uid))
        with pytest.raises(Exception):                    # the database itself refuses a repeat
            await s.commit()


async def test_filters_scam_and_low_relevance(env):
    ok = await make_lead(relevance=70)
    low = await make_lead(relevance=20)
    scam = await make_lead(relevance=90, scam="high")
    medium = await make_lead(relevance=90, scam="medium")
    n, _ = await AU.notify_leads(ADMIN, [ok, low, scam, medium], 50)
    assert n == 2
    texts = " ".join(m["text"] for m in env.sent)
    assert "Scam risk medium" in texts                    # medium is sent, with its flags shown


async def test_more_than_five_becomes_one_digest(env):
    ids = [await make_lead(relevance=60 + i) for i in range(7)]
    n, err = await AU.notify_leads(ADMIN, ids, 50)
    assert n == 7 and err == "" and len(env.sent) == 1
    assert env.sent[0]["text"].startswith("🆕 <b>7 new Web3 leads</b>")
    assert await AU.notify_leads(ADMIN, ids, 50) == (0, "")


async def test_forbidden_is_recorded_and_lead_can_be_sent_later(env):
    lid = await make_lead()
    env.send_error = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot can't initiate conversation")
    n, err = await AU.notify_leads(ADMIN, [lid], 50)
    assert n == 0 and "press Start" in err
    env.send_error = None
    n, _ = await AU.notify_leads(ADMIN, [lid], 50)          # reservation was released: delivered now, once
    assert n == 1


async def test_per_chat_pacing(env, monkeypatch):
    monkeypatch.setattr(AU, "PER_CHAT_GAP", 0.2)
    ids = [await make_lead() for _ in range(3)]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await AU.notify_leads(ADMIN, ids, 50)
    assert loop.time() - t0 >= 0.39                       # 3 messages -> at least 2 gaps


# ------------------------------------------------------------------ reminder: edit vs send
async def test_reminder_edits_existing_message(env):
    await AU.set_mode(ADMIN, "start")
    assert await AU.send_reminder(ADMIN) == "sent"           # first time: new message, pinned
    mid = env.sent[-1]["id"]
    assert env.pins == [mid]
    assert await AU.send_reminder(ADMIN) == "edited"         # afterwards: edit, no new message
    assert len(env.sent) == 1 and env.edits[-1]["message_id"] == mid
    assert "Running" in env.edits[-1]["text"]
    env.edit_error = TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    assert await AU.send_reminder(ADMIN) == "edited" and len(env.sent) == 1
    env.edit_error = TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")
    assert await AU.send_reminder(ADMIN) == "sent"           # deleted by the user -> send new, remember it
    assert len(env.sent) == 2
    async with SessionLocal() as s:
        assert (await s.get(AutomationState, ADMIN)).status_message_id == env.sent[-1]["id"]


async def test_reminder_interval_respected(env):
    await AU.set_mode(ADMIN, "start")
    await AU.update_settings(ADMIN, reminder_interval_minutes=3)
    async with SessionLocal() as s:
        a = await s.get(AutomationState, ADMIN)
        a.next_run_at = utcnow() + timedelta(hours=1)
        a.last_reminder_at = utcnow() - timedelta(minutes=1)
        await s.commit()
    assert ADMIN not in (await AU.tick())["reminded"]
    assert ADMIN in (await AU.tick(utcnow() + timedelta(minutes=3)))["reminded"]


# ------------------------------------------------------------------ bot controls are admin-only
def _cb(uid, data, message_id=555):
    c = MagicMock()
    c.from_user.id, c.from_user.is_bot, c.data = uid, False, data
    c.answer = AsyncMock()
    c.message.message_id = message_id
    c.message.answer = AsyncMock()
    c.message.edit_reply_markup = AsyncMock()
    return c


async def test_bot_callbacks_admin_only(env):
    c = _cb(USER, "au|start")
    await BH.cb_auto(c)
    assert c.answer.call_args.kwargs.get("show_alert") and (await AU.status(ADMIN))["state"] == "stopped"
    async with SessionLocal() as s:
        assert await s.get(AutomationState, USER) is None   # nothing created for the non-admin
    c = _cb(ADMIN, "au|start")
    await BH.cb_auto(c)
    assert (await AU.status(ADMIN))["state"] == "running"
    assert env.edits and env.edits[-1]["message_id"] == 555  # the status message under the button is edited
    await BH.cb_auto(_cb(ADMIN, "au|pause"))
    assert (await AU.status(ADMIN))["state"] == "paused"
    await BH.cb_auto(_cb(ADMIN, "au|stop"))
    assert (await AU.status(ADMIN))["state"] == "stopped"


async def test_lead_buttons_check_admin_and_owner(env):
    mine = await make_lead()
    theirs = await make_lead(owner=OTHER_ADMIN)
    c = _cb(USER, f"an|s|{mine}")
    await BH.cb_lead(c)
    assert c.answer.call_args.kwargs.get("show_alert")
    c = _cb(ADMIN, f"an|s|{theirs}")                       # another admin's lead
    await BH.cb_lead(c)
    assert "not found" in c.answer.call_args.args[0].lower()
    await BH.cb_lead(_cb(ADMIN, f"an|s|{mine}"))
    async with SessionLocal() as s:
        assert (await s.get(Lead, mine)).status == "saved"
    c = _cb(ADMIN, f"an|d|{mine}")
    await BH.cb_lead(c)
    txt = c.message.answer.call_args.args[0]
    assert "Draft for" in txt and "Not sent" in txt        # draft only, never sent
    await BH.cb_lead(_cb(ADMIN, f"an|k|{mine}"))
    async with SessionLocal() as s:
        assert (await s.get(Lead, mine)).status == "skipped"
    scam = await make_lead(scam="high")
    c = _cb(ADMIN, f"an|d|{scam}")
    await BH.cb_lead(c)
    assert "scam" in c.answer.call_args.args[0].lower()


async def test_bot_commands_admin_only(env):
    m = MagicMock()
    m.from_user.id, m.from_user.is_bot, m.text = USER, False, "/auto_start"
    m.answer = AsyncMock()
    await BH.cmd_auto(m)
    assert m.answer.call_args.args[0] == BH.NOT_ADMIN
    m.from_user.id = ADMIN
    m.answer = AsyncMock(return_value=MagicMock(message_id=777))
    await BH.cmd_auto(m)
    assert (await AU.status(ADMIN))["state"] == "running"
    async with SessionLocal() as s:
        assert (await s.get(AutomationState, ADMIN)).status_message_id == 777
    m.text = "/auto_status@Test_jobs_bot"
    await BH.cmd_auto(m)
    assert "Job agent automation" in m.answer.call_args.args[0]


# ------------------------------------------------------------------ API (Mini App)
@pytest.fixture()
def client(env, monkeypatch):
    from app import main
    calls = []

    async def fake_discovery(owner_id, sources, custom_query="", trigger="manual"):
        calls.append(trigger)
        return {"new": 0, "errors": [], "new_ids": [], "by_source": {}}
    monkeypatch.setattr(A, "run_discovery", fake_discovery)
    with TestClient(main.app) as c:
        AU.set_bot(env)          # lifespan replaced it with the app's bot
        c.calls = calls
        yield c


def _wait_idle(c):
    import time
    for _ in range(50):
        if not c.get("/api/agent/automation", headers=h()).json()["running_now"]:
            return
        time.sleep(0.05)


def test_automation_api_admin_only_and_controls(client):
    assert client.get("/api/agent/automation", headers=h(USER)).status_code == 403
    assert client.post("/api/agent/automation/start", headers=h(USER)).status_code == 403
    assert client.post("/api/agent/automation/app-open", headers=h(USER)).status_code == 403
    assert client.get("/api/agent/automation").status_code == 401
    st = client.get("/api/agent/automation", headers=h()).json()
    assert st["state"] == "stopped" and st["search_interval_minutes"] == 15 and st["reminder_interval_minutes"] == 1
    assert client.post("/api/agent/automation/start", headers=h()).json()["state"] == "running"
    assert client.post("/api/agent/automation/pause", headers=h()).json()["state"] == "paused"
    assert client.post("/api/agent/automation/bogus", headers=h()).status_code == 400
    r = client.post("/api/agent/automation/run", headers=h()).json()
    assert r["started"] is True
    _wait_idle(client)
    assert client.post("/api/agent/automation/stop", headers=h()).json()["state"] == "stopped"
    logs = client.get("/api/agent/automation/logs", headers=h()).json()["logs"]
    assert any("Automation" in l["message"] for l in logs)
    assert client.get("/api/agent/dashboard", headers=h()).json()["automation"]["state"] == "stopped"
    assert client.get("/api/agent/dashboard", headers=h(USER)).json()["automation"] is None


def test_app_open_debounce(client):
    r = client.post("/api/agent/automation/app-open", headers=h()).json()
    assert r["started"] is False                           # automation stopped: opening the app does nothing
    client.post("/api/agent/automation/start", headers=h())
    r = client.post("/api/agent/automation/app-open", headers=h()).json()
    assert r["started"] is True
    _wait_idle(client)
    r = client.post("/api/agent/automation/app-open", headers=h()).json()
    assert r["started"] is False and "5 minutes" in r["message"]   # ran < 5 min ago

    async def age():
        async with SessionLocal() as s:
            a = await s.get(AutomationState, ADMIN)
            a.last_run_started_at = a.last_run_at = utcnow() - timedelta(minutes=6)
            await s.commit()
    client.portal.call(age)
    assert client.post("/api/agent/automation/app-open", headers=h()).json()["started"] is True
    _wait_idle(client)
    assert client.calls.count("auto") == 2


def test_automation_settings_roundtrip_and_validation(client):
    assert client.put("/api/agent/automation/settings", json={"search_interval_minutes": 4}, headers=h()).status_code == 422
    assert client.put("/api/agent/automation/settings", json={"reminder_interval_minutes": 0}, headers=h()).status_code == 422
    assert client.put("/api/agent/automation/settings", json={"sources": ["myspace"]}, headers=h()).status_code == 400
    r = client.put("/api/agent/automation/settings", json={"search_interval_minutes": 30, "reminder_interval_minutes": 2,
                   "sources": ["google", "x"], "min_relevance": 65}, headers=h())
    assert r.status_code == 200
    st = client.get("/api/agent/automation", headers=h()).json()
    assert (st["search_interval_minutes"], st["reminder_interval_minutes"], st["sources"], st["min_relevance"]) == \
        (30, 2, ["google", "x"], 65)
    client.put("/api/agent/automation/settings", json={"min_relevance": 40}, headers=h())   # partial update
    st = client.get("/api/agent/automation", headers=h()).json()
    assert st["search_interval_minutes"] == 30 and st["min_relevance"] == 40
