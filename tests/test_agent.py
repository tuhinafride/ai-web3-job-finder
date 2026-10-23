"""AI job discovery + outreach agent: qualification, dedupe, drafts, approval, guarded sending."""
import types
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import utcnow
from app.outreach import agent as A
from app.outreach import drafts as D
from app.outreach import qualify as Q
from app.outreach import search as S
from app.outreach import tg
from tests.test_app import make_init_data

OWNER, STRANGER = 999, 42


def h(uid=OWNER):
    return {"X-Telegram-Init-Data": make_init_data(uid)}


# ------------------------------------------------------------------ qualification (pure)
MOD_POST = """🚀 We're hiring! NovaChain (DeFi L2) is looking for an experienced Discord Moderator.
• Moderate our 50K Discord and Telegram
• Answer user questions and escalate bugs
Pay: $600/month in USDT, fully remote, open worldwide.
Apply: https://forms.gle/abc123 or DM @nova_hr_lead on Telegram. Deadline: Nov 15"""

SCAM_POST = """Hiring Telegram moderators for a crypto project! Earn $500 per day, no experience needed.
To start, pay a small registration fee of 20 USDT and connect your wallet to verify. Send your seed phrase for KYC."""


def test_rules_extract_role_pay_contacts_deadline():
    q = Q.rule_qualify(MOD_POST, preferred_roles=["moderator"], posted_at=utcnow() - timedelta(days=1))
    assert q.role == "moderator" and q.is_hiring and q.web3
    assert q.pay_status == "paid" and "$600" in q.pay_detail
    assert q.remote == "yes"
    assert q.deadline.startswith("Nov 15")
    assert {"platform": "telegram", "handle": "nova_hr_lead", "role_hint": "mentioned in post"} in q.contacts
    assert q.apply_method.startswith("https://forms.gle/")
    assert q.scam_risk == "low" and q.freshness == "current"
    assert q.relevance >= 80


def test_scam_signals_flagged_and_score_crushed():
    q = Q.rule_qualify(SCAM_POST, preferred_roles=["moderator"])
    assert q.scam_risk == "high"
    joined = " ".join(q.scam_flags)
    for word in ("seed", "fee", "wallet"):
        assert word in joined.lower()
    assert q.relevance < 30


def test_super_moderator_and_unpaid_detection():
    q = Q.rule_qualify("Web3 project looking for a Super Moderator. This is a volunteer role, unpaid.")
    assert q.role == "super_moderator" and q.pay_status == "unpaid"


def test_old_post_marked_old():
    q = Q.rule_qualify(MOD_POST, posted_at=utcnow() - timedelta(days=90))
    assert q.freshness == "old"


def test_contacts_skip_bots_and_generic_words():
    c = Q.extract_contacts("Contact @admin or @hiring or @nova_jobs_bot, email team@nova.xyz, t.me/real_founder")
    handles = {x["handle"] for x in c}
    assert "real_founder" in handles and "team@nova.xyz" in handles
    assert not {"admin", "hiring", "nova_jobs_bot"} & handles


def test_uid_normalises_tracking_and_twitter_domain():
    a = Q.lead_uid("https://twitter.com/nova/status/1?utm_source=x")
    b = Q.lead_uid("https://x.com/nova/status/1")
    assert a == b


# ------------------------------------------------------------------ drafts (pure)
def _profile():
    from app.outreach.models import DEFAULT_PROFILE
    return types.SimpleNamespace(**DEFAULT_PROFILE)


def _lead(**kw):
    base = dict(project="NovaChain", title="Discord Moderator", role="moderator", raw_text=MOD_POST,
                responsibilities="", requirements="", pay_status="paid", pay_detail="$600/month",
                apply_method="", source="x")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_template_personalised_without_inventing():
    text = D.template_draft(_profile(), _lead())
    assert text.startswith("Hi NovaChain team,")
    assert "if NovaChain currently" in text
    assert "Discord / Telegram Moderator role" in text
    assert "4+ years" in text


def test_ai_draft_with_invented_numbers_rejected():
    p, l = _profile(), _lead()
    assert D.safe_ai_text("Hi NovaChain, I have 4+ years and ran 100K+ communities. Any paid moderator role?", p, l)
    assert not D.safe_ai_text("Hi NovaChain, I have 9 years of experience at Binance.", p, l)
    assert not D.safe_ai_text("Hi [Name], ...", p, l)


@pytest.mark.asyncio
async def test_ai_fallback_to_template_when_llm_lies(monkeypatch):
    from app.outreach import llm

    async def fake(system, user, **kw):
        return {"message": "Hi NovaChain, I worked 12 years at Coinbase."}
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", fake)
    text, by = await D.generate(_profile(), _lead())
    assert by == "template" and "Coinbase" not in text


# ------------------------------------------------------------------ API + flow (fake providers, fake Telegram)
SENT = []


@pytest.fixture()
def fakes(monkeypatch):
    from app import main
    SENT.clear()
    monkeypatch.setattr(main.settings, "webhook_secret", "test-webhook-secret-xyz")

    async def fake_web_search(query, num=10):
        return [S.RawLead("google", "https://novachain.xyz/careers/moderator", "NovaChain hiring Discord Moderator",
                          MOD_POST, posted_at=utcnow() - timedelta(days=2)),
                S.RawLead("google", "https://scam.example/mods", "Telegram mods wanted", SCAM_POST),
                S.RawLead("google", "https://news.example/eth", "ETH price today",
                          "Ethereum price rose 3% today as traders watched the ETF news and on-chain data.")]

    async def fake_enrich(lead):
        lead.checked = True
        return lead

    async def fake_x(query="", max_results=25):
        return [S.RawLead("x", "https://x.com/nova/status/1", "", MOD_POST, project="NovaChain",
                          author_x="nova_founder", checked=True)]

    async def fake_send(owner_id, username, text):
        if username == "private_guy":
            raise tg.SendBlocked("privacy", "@private_guy doesn't accept messages from you.")
        if username == "flood_me":
            raise tg.SendBlocked("peer_flood", "limited", True)
        SENT.append((username, text))
        return 1

    monkeypatch.setattr(S, "web_search", fake_web_search)
    monkeypatch.setattr(S, "enrich_page", fake_enrich)
    monkeypatch.setattr(S, "x_search", fake_x)
    monkeypatch.setattr(tg, "send_dm", fake_send)
    return main


@pytest.fixture()
def client(fakes):
    with TestClient(fakes.app) as c:
        yield c


def test_agent_endpoints_owner_only(client):
    assert client.get("/api/agent/dashboard").status_code == 401
    for path in ["/api/agent/dashboard", "/api/agent/leads", "/api/agent/drafts", "/api/agent/logs",
                 "/api/agent/profile", "/api/agent/settings", "/api/agent/contacts"]:
        assert client.get(path, headers=h(STRANGER)).status_code == 403
    assert client.post("/api/agent/search", json={}, headers=h(STRANGER)).status_code == 403


@pytest.mark.asyncio
async def test_discovery_dedupes_filters_and_flags(fakes):
    from app.db import init_db
    await init_db()
    s1 = await A.run_discovery(OWNER, ["google", "x"])
    assert s1["new"] >= 2 and s1["irrelevant"] >= 1       # ETH news dropped
    s2 = await A.run_discovery(OWNER, ["google", "x"])
    assert s2["new"] == 0 and s2["duplicate"] >= 2        # nothing duplicated on re-run


def test_full_draft_approve_send_flow_and_no_repeat(client):
    client.post("/api/agent/search", json={"sources": ["google"]}, headers=h())
    import time
    for _ in range(50):
        if not client.get("/api/agent/dashboard", headers=h()).json()["agent"]["running"]:
            break
        time.sleep(0.1)
    leads = client.get("/api/agent/leads?sort=relevance", headers=h()).json()["leads"]
    good = next(l for l in leads if l["scam_risk"] == "low" and l["project"] or l["title"])
    scam = [l for l in client.get("/api/agent/leads", headers=h()).json()["leads"] if l["scam_risk"] == "high"]
    assert scam, "scam lead should be kept but flagged"
    assert client.post(f"/api/agent/leads/{scam[0]['id']}/draft", json={}, headers=h()).status_code == 409

    d = client.post(f"/api/agent/leads/{good['id']}/draft", json={}, headers=h()).json()
    assert d["channel"] == "telegram" and d["recipient"] == "nova_hr_lead" and d["status"] == "pending"
    assert d["source_url"] and d["project"] is not None and d["text"]
    assert SENT == []                                     # nothing sent before approval

    # edit then approve & send
    r = client.post(f"/api/agent/drafts/{d['id']}", json={"action": "edit", "text": d["text"] + "\n\nBest, Tuhin"}, headers=h())
    assert r.json()["ok"]
    r = client.post(f"/api/agent/drafts/{d['id']}", json={"action": "approve"}, headers=h()).json()
    assert r["ok"] and r["status"] == "sent" and SENT[0][0] == "nova_hr_lead" and SENT[0][1].endswith("Best, Tuhin")

    # same recipient again -> skipped, never sent twice
    d2 = client.post(f"/api/agent/leads/{good['id']}/draft", json={}, headers=h()).json()
    assert d2["status"] == "skipped" or d2["recipient"] != "nova_hr_lead"
    sent_list = client.get("/api/agent/drafts?status=sent", headers=h()).json()["drafts"]
    assert len(sent_list) == 1
    assert client.get(f"/api/agent/leads/{good['id']}", headers=h()).json()["status"] == "applied"
    logs = client.get("/api/agent/logs", headers=h()).json()["logs"]
    assert any("Sent draft" in l["message"] for l in logs)


@pytest.mark.asyncio
async def test_send_guards_stop_interval_daily_limit(fakes):
    from app.db import SessionLocal
    from app.outreach.models import Draft, Lead

    async with SessionLocal() as s:
        lead = Lead(owner_id=OWNER, uid="g1", source="x", source_url="https://x.com/a/status/9", title="Mod", role="moderator")
        s.add(lead)
        await s.flush()
        ids = []
        for name in ["alice_web3", "bob_web3", "private_guy", "flood_me"]:
            d = Draft(owner_id=OWNER, lead_id=lead.id, channel="telegram", recipient=name, text="hi", status="approved")
            s.add(d)
            await s.flush()
            ids.append(d.id)
        st = await A.get_state(s, OWNER)
        st.stopped, st.last_sent_at, st.min_interval_sec, st.daily_limit = False, None, 60, 10
        await s.commit()

    # stopped agent sends nothing
    await A.stop_agent(OWNER, True)
    assert (await A.try_send(OWNER, ids[0]))["code"] == "stopped"
    await A.stop_agent(OWNER, False)

    assert (await A.try_send(OWNER, ids[0]))["ok"]
    r = await A.try_send(OWNER, ids[1])                   # too soon after the last send
    assert r["code"] == "interval" and r["wait"] > 0

    async with SessionLocal() as s:                       # pretend the interval passed
        (await A.get_state(s, OWNER)).last_sent_at = utcnow() - timedelta(minutes=10)
        await s.commit()
    r = await A.try_send(OWNER, ids[2])
    assert r["code"] == "privacy"
    async with SessionLocal() as s:
        assert (await s.get(Draft, ids[2])).status == "failed"
        (await A.get_state(s, OWNER)).last_sent_at = utcnow() - timedelta(minutes=10)
        await s.commit()

    r = await A.try_send(OWNER, ids[3])                   # Telegram spam limit -> agent auto-stops
    assert r["code"] == "peer_flood"
    async with SessionLocal() as s:
        assert (await A.get_state(s, OWNER)).stopped is True
        st = await A.get_state(s, OWNER)
        st.stopped, st.daily_limit, st.last_sent_at = False, 1, utcnow() - timedelta(minutes=10)
        await s.commit()
    r = await A.try_send(OWNER, ids[1])                   # daily cap (1) already used by alice
    assert r["code"] == "daily_limit"


def test_stop_button_blocks_search(client):
    client.post("/api/agent/stop", json={"stop": True}, headers=h())
    assert client.post("/api/agent/search", json={"sources": ["google"]}, headers=h()).status_code == 409
    assert client.get("/api/agent/dashboard", headers=h()).json()["agent"]["stopped"] is True
    client.post("/api/agent/stop", json={"stop": False}, headers=h())


def test_profile_and_settings_roundtrip(client):
    p = client.get("/api/agent/profile", headers=h()).json()
    assert p["name"] == "Tuhin Afride" and "moderator" in p["preferred_roles"]
    p["skills"] += ", Zealy campaigns"
    assert client.put("/api/agent/profile", json={k: p[k] for k in ("name", "location", "headline", "experience",
                      "skills", "preferred_roles", "links", "template")}, headers=h()).json()["ok"]
    assert "Zealy" in client.get("/api/agent/profile", headers=h()).json()["skills"]
    r = client.put("/api/agent/settings", json={"daily_limit": 99}, headers=h())
    assert r.status_code == 422                            # caps enforced server-side
    assert client.put("/api/agent/settings", json={"daily_limit": 8, "min_interval_sec": 240,
                      "queries": "web3 moderator hiring", "tg_channels": "@web3jobs_example"}, headers=h()).json()["ok"]
    st = client.get("/api/agent/settings", headers=h()).json()
    assert st["daily_limit"] == 8 and st["tg_channels"] == "web3jobs_example"


def test_board_job_import_and_manual_draft(client):
    import asyncio
    from app.db import Job, SessionLocal

    async def add_job():
        async with SessionLocal() as s:
            j = Job(uid="test:cm1", source="greenhouse", source_name="Test", source_type="company_ats",
                    title="Community Manager", company="Uniswap Labs", url="https://jobs.example/cm1",
                    apply_url="https://jobs.example/cm1/apply", description="Lead our web3 community. Remote.")
            s.add(j)
            await s.commit()
            return j.id
    jid = client.portal.call(add_job)
    r = client.post(f"/api/agent/jobs/{jid}/import", headers=h())
    assert r.status_code == 200
    lead = client.get(f"/api/agent/leads/{r.json()['lead_id']}", headers=h()).json()
    assert lead["source"] == "board" and lead["project"] == "Uniswap Labs" and lead["source_checked"]
    d = client.post(f"/api/agent/leads/{lead['id']}/draft", json={}, headers=h()).json()
    assert d["channel"] == "manual" and d["recipient"] == ""     # no public contact: manual apply
    assert client.post(f"/api/agent/drafts/{d['id']}", json={"action": "mark_sent"}, headers=h()).json()["ok"]
