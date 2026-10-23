import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest

from app.bot import format as fmt
from app.bot import handlers
from app.classify import classify, extract_requirements, html_to_text, is_web3
from app.db import Job, SessionLocal, init_db, utcnow
from app.services import match_score
from app.sources.providers import RawJob
from app.webapp_auth import InitDataError, validate_init_data

TOKEN = os.environ["BOT_TOKEN"]


def make_init_data(user_id=42, token=TOKEN, auth_date=None, **extra):
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Test", "username": "tester"}), **extra}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# ---------------- auth ----------------
def test_init_data_valid():
    assert validate_init_data(make_init_data(7), TOKEN)["id"] == 7


def test_init_data_wrong_token_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data(7, token="999:other"), TOKEN)


def test_init_data_tampered_rejected():
    data = make_init_data(7).replace("%22id%22%3A+7", "%22id%22%3A+8")
    with pytest.raises(InitDataError):
        validate_init_data(data, TOKEN)


def test_init_data_expired_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data(7, auth_date=int(time.time()) - 200000), TOKEN, max_age=86400)


# ---------------- text helpers ----------------
def test_classify_roles():
    assert classify("Community Manager (Discord)") == "community"
    assert classify("Telegram Moderator") == "moderator"
    assert classify("Senior Solidity Engineer") == "developer"
    assert classify("Customer Support Specialist") == "support"
    assert classify("Business Development Lead, Asia") == "bd"
    assert classify("Social Media Manager") == "social"
    assert classify("Growth Marketing Manager") == "growth"


def test_requirements_extraction():
    text = html_to_text("<p>About</p><h3>Requirements</h3><ul><li>3+ years community</li>"
                        "<li>Fluent English</li></ul><h3>Benefits</h3><ul><li>Remote</li></ul>")
    assert extract_requirements(text) == ["3+ years community", "Fluent English"]


def test_salary_extraction():
    from app.classify import extract_salary
    assert extract_salary("The base pay range is $120,000 - $150,000 USD per year.").startswith("$120,000 - $150,000")
    assert extract_salary("No pay info here") == ""


def test_web3_filter():
    assert is_web3("DeFi protocol hiring")
    assert not is_web3("Bakery assistant", "We sell bread")


def test_escaping_in_cards():
    job = Job(id=1, title="<script>x</script>", company="A&B", location="Remote", remote=True, salary="",
              category="other", requirements="", description="", url="https://e.com/a",
              apply_url="https://e.com/b", source_name="X", source_type="aggregator", tags="")
    card = fmt.job_card(job)
    assert "<script>" not in card and "&lt;script&gt;" in card
    assert "verify" in card.lower()  # aggregator listings carry the verify warning


def test_match_score():
    job = Job(title="Community Manager", category="community", tags="discord", description="telegram discord",
              remote=True)
    assert match_score(job, ["community"], ["discord"], False) == 100
    assert match_score(job, ["developer"], [], False) == 0
    job.remote = False
    assert match_score(job, ["community"], [], True) == 0


# ---------------- API ----------------
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    import asyncio

    async def seed():
        await init_db()
        rj = RawJob(source="test", source_name="Test careers", source_type="company_ats", external_id="1",
                    title="Community Manager", company="ChainCo", url="https://example.com/job/1",
                    apply_url="https://example.com/job/1/apply", location="Remote", remote=True,
                    salary="$60,000–$80,000 / year", tags=["community"],
                    description="Requirements\n• Discord experience\n• Fluent English")
        async with SessionLocal() as s:
            j = Job(**rj.to_row(), first_seen=utcnow(), last_seen=utcnow())
            s.add(j)
            await s.commit()
            return j.id
    return asyncio.run(seed())


def h(uid=42):
    return {"X-Telegram-Init-Data": make_init_data(uid)}


def test_api_requires_auth(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"X-Telegram-Init-Data": "user=%7B%7D&hash=abc"}).status_code == 401


def test_api_jobs_profile_saved(client, seeded):
    r = client.get("/api/jobs?q=community", headers=h())
    assert r.status_code == 200 and r.json()["total"] >= 1
    job = r.json()["jobs"][0]
    assert job["requirements"] == ["Discord experience", "Fluent English"]
    assert job["source_type"] == "company_ats"

    r = client.put("/api/profile", headers=h(), json={"roles": ["community", "bogus"], "skills": ["Discord"],
                                                       "remote_only": True, "alerts_enabled": True})
    assert r.json()["roles"] == ["community"] and r.json()["alerts_enabled"] is True

    r = client.get("/api/jobs?for_me=true", headers=h())
    assert r.json()["jobs"][0]["match_score"] == 100

    assert client.post(f"/api/saved/{seeded}", headers=h()).json()["saved"] is True
    assert len(client.get("/api/saved", headers=h()).json()["jobs"]) == 1
    assert len(client.get("/api/saved", headers=h(43)).json()["jobs"]) == 0  # per-user isolation
    assert client.delete(f"/api/saved/{seeded}", headers=h()).json()["saved"] is False
    assert client.post("/api/saved/99999", headers=h()).status_code == 404


def test_webhook_rejects_bad_secret(client):
    assert client.post("/telegram/webhook", json={}).status_code == 403


def test_miniapp_served(client):
    r = client.get("/app/")
    assert r.status_code == 200 and "Web3 Job Finder" in r.text


# ---------------- admin protection ----------------
def _msg(user_id):
    m = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    return m


async def test_admin_commands_blocked_for_non_admin():
    m = _msg(1)
    cmd = MagicMock(args="1")
    await handlers.cmd_post(m, cmd)
    assert "Admins only" in m.answer.call_args.args[0]
    m2 = _msg(1)
    await handlers.cmd_refresh(m2, MagicMock())
    assert "Admins only" in m2.answer.call_args.args[0]


async def test_publish_callback_rechecks_admin():
    c = MagicMock()
    c.from_user.id = 1
    c.data = "pub|j|1"
    c.answer = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await handlers.cb_publish(c, MagicMock(), bot)
    bot.send_message.assert_not_called()


def test_admin_ids_parsing():
    assert handlers.is_admin(999) and not handlers.is_admin(1)


def test_no_token_in_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = __import__("re").compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
    for p in root.rglob("*"):
        if p.suffix in {".py", ".js", ".html", ".yaml", ".md", ".example", ".txt"} and "tests" not in p.parts:
            assert not pattern.search(p.read_text(errors="ignore")), f"possible bot token in {p}"
