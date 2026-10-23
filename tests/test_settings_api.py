"""Agent Settings flow (UI form <-> /api/agent/settings + /profile) and a sweep of every /api/agent and /api/admin
endpoint, as admin, as a normal user and unauthenticated."""
import pytest
from fastapi.testclient import TestClient

from app.outreach.models import DEFAULT_QUERIES
from tests.test_app import make_init_data

ADMIN, USER = 999, 4321


def h(uid=ADMIN):
    return {"X-Telegram-Init-Data": make_init_data(uid)}


@pytest.fixture()
def client():
    from app import main
    from app.outreach import automation as AU
    with TestClient(main.app) as c:
        AU.set_bot(None)          # never talk to the real Telegram API from tests
        yield c


def test_partial_settings_save_keeps_other_fields(client):
    full = {"daily_limit": 7, "min_interval_sec": 300, "auto_search_hours": 6,
            "queries": "web3 moderator hiring\ncrypto CM job", "tg_channels": "@web3jobs_example"}
    assert client.put("/api/agent/settings", json=full, headers=h()).json()["ok"]
    # BUG (fixed): a partial save used to reset every omitted field to its default
    r = client.put("/api/agent/settings", json={"daily_limit": 9}, headers=h()).json()
    s = r["settings"]
    assert s["daily_limit"] == 9 and s["min_interval_sec"] == 300 and s["auto_search_hours"] == 6
    assert s["queries"] == "web3 moderator hiring\ncrypto CM job" and s["tg_channels"] == "web3jobs_example"
    again = client.get("/api/agent/settings", headers=h()).json()
    assert {k: again[k] for k in full} == {k: s[k] for k in full}      # reload == what was saved


def test_channels_normalised_validated_and_persisted(client):
    raw = ("@web3jobs_example\nt.me/cryptojobslist\nhttps://t.me/s/Remote3Jobs/55, https://t.me/web3hiring?x=1\n"
           "not a channel!\n@ab\n@WEB3JOBS_example")
    r = client.put("/api/agent/settings", json={"tg_channels": raw}, headers=h()).json()
    assert r["settings"]["tg_channels"].split("\n") == ["web3jobs_example", "cryptojobslist", "Remote3Jobs", "web3hiring"]
    assert any("not valid" in w.lower() for w in r["warnings"])
    assert client.get("/api/agent/settings", headers=h()).json()["tg_channels"].count("\n") == 3
    many = "\n".join(f"@channel_{i:02d}" for i in range(20))
    r = client.put("/api/agent/settings", json={"tg_channels": many}, headers=h()).json()
    assert len(r["settings"]["tg_channels"].split("\n")) == 15 and any("15" in w for w in r["warnings"])


def test_empty_queries_restore_defaults(client):
    r = client.put("/api/agent/settings", json={"queries": "  \n \n"}, headers=h()).json()
    assert r["settings"]["queries"].split("\n") == DEFAULT_QUERIES and r["warnings"]
    r = client.put("/api/agent/settings", json={"queries": "a\na\nb"}, headers=h()).json()
    assert r["settings"]["queries"] == "a\nb"                   # duplicates dropped


def test_settings_limits_validated(client):
    for bad in ({"daily_limit": 0}, {"daily_limit": 31}, {"min_interval_sec": 59}, {"auto_search_hours": 49},
                {"daily_limit": "abc"}, {"queries": "x" * 2001}):
        r = client.put("/api/agent/settings", json=bad, headers=h())
        assert r.status_code == 422, bad
        assert r.json()["detail"][0]["loc"][-1] in bad           # the UI can name the field


def test_non_admin_cannot_set_admin_fields(client):
    r = client.put("/api/agent/settings", json={"auto_search_hours": 5, "tg_channels": "@somechannel"},
                   headers=h(USER)).json()
    assert r["settings"]["auto_search_hours"] == 0 and r["settings"]["tg_channels"] == ""
    assert r["warnings"]


def test_profile_roundtrip_and_validation(client):
    p = client.get("/api/agent/profile", headers=h()).json()
    body = {k: p[k] for k in ("name", "location", "headline", "experience", "skills", "preferred_roles", "links", "template")}
    body["preferred_roles"] = ["moderator", "community", "moderator", "not_a_role"]
    r = client.put("/api/agent/profile", json=body, headers=h()).json()
    assert r["ok"] and r["profile"]["preferred_roles"] == ["moderator", "community"]
    assert client.get("/api/agent/profile", headers=h()).json()["preferred_roles"] == ["moderator", "community"]
    # BUG (fixed): an empty template was accepted and produced empty drafts
    assert client.put("/api/agent/profile", json={**body, "template": "   "}, headers=h()).status_code == 400
    assert client.put("/api/agent/profile", json={**body, "template": "x" * 1501}, headers=h()).status_code == 422


def test_every_agent_and_admin_endpoint(client):
    """Sweep: each endpoint answers as expected for admin / user / no auth (no 500s anywhere)."""
    get_paths = ["/api/agent/dashboard", "/api/agent/leads", "/api/agent/leads?sort=posted&pay=paid&min_relevance=50",
                 "/api/agent/drafts", "/api/agent/drafts?status=sent,failed", "/api/agent/contacts",
                 "/api/agent/contacts?platform=telegram", "/api/agent/logs", "/api/agent/logs?kind=auto",
                 "/api/agent/profile", "/api/agent/settings"]
    for p in get_paths:
        assert client.get(p).status_code == 401, p
        assert client.get(p, headers=h()).status_code == 200, p
        assert client.get(p, headers=h(USER)).status_code == 200, p
    for p in ["/api/agent/contacts?limit=0", "/api/agent/logs?limit=0", "/api/agent/leads?limit=0"]:
        assert client.get(p, headers=h()).status_code == 422, p   # BUG (fixed): limit=0 / -1 returned everything
    admin_only = ["/api/agent/automation", "/api/agent/automation/logs", "/api/admin/stats", "/api/admin/users",
                  "/api/admin/users.csv", "/api/admin/audit", "/api/admin/referral-config"]
    for p in admin_only:
        assert client.get(p).status_code == 401, p
        assert client.get(p, headers=h(USER)).status_code == 403, p
        assert client.get(p, headers=h()).status_code == 200, p
    assert client.get("/api/agent/leads/999999", headers=h()).status_code == 404
    assert client.patch("/api/agent/leads/999999", json={"status": "saved"}, headers=h()).status_code == 404
    assert client.post("/api/agent/leads/999999/draft", json={}, headers=h()).status_code == 404
    assert client.post("/api/agent/drafts/999999", json={"action": "approve"}, headers=h()).status_code == 404
    assert client.post("/api/agent/jobs/999999/import", headers=h()).status_code == 404
    assert client.post("/api/agent/search", json={"sources": []}, headers=h()).status_code == 400
    assert client.post("/api/agent/search", json={"sources": ["telegram"]}, headers=h(USER)).status_code == 403
    assert client.post("/api/agent/stop", json={"stop": False}, headers=h()).json() == {"stopped": False}
    assert client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": 5},
                      headers=h()).status_code == 200
    assert client.put("/api/admin/referral-config", json={"enabled": True, "points_per_referral": 5},
                      headers=h(USER)).status_code == 403
    assert client.post(f"/api/admin/users/{ADMIN}/status", json={"status": "suspended"}, headers=h()).status_code == 400
    assert client.post("/api/admin/users/123456789/status", json={"status": "suspended"}, headers=h()).status_code == 404
