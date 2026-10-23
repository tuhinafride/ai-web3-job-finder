"""Personal-account login flow, tested against a fake Telethon client (no network)."""
import types

import pytest
from fastapi.testclient import TestClient

from app import userclient as uc
from app.db import init_db
from tests.test_app import make_init_data

OWNER = 999          # in ADMIN_IDS (conftest)
STRANGER = 42


class FakeErr(Exception):
    pass


def err(name):
    return type(name, (Exception,), {})()


class FakeSession:
    def save(self):
        return "SESSION-STRING-SECRET"


class FakeClient:
    instances = []
    two_fa = False
    password = "hunter2"
    code = "73914"
    authorized = True

    def __init__(self, session, api_id, api_hash):
        self.session = FakeSession()
        self.api_id, self.api_hash = api_id, api_hash
        self.signed = False
        self.logged_out = False
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_code_request(self, phone):
        if phone == "+10000000000":
            raise err("PhoneNumberInvalidError")
        return types.SimpleNamespace(phone_code_hash="HASH", type=types.SimpleNamespace())

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != FakeClient.password:
                raise err("PasswordHashInvalidError")
            self.signed = True
            return
        assert phone_code_hash == "HASH"
        if code != FakeClient.code:
            raise err("PhoneCodeInvalidError")
        if FakeClient.two_fa:
            raise err("SessionPasswordNeededError")
        self.signed = True

    async def get_me(self):
        return types.SimpleNamespace(first_name="Tuhin", last_name="", username="tuhin", id=777)

    async def is_user_authorized(self):
        return FakeClient.authorized

    async def log_out(self):
        self.logged_out = True
        return True


@pytest.fixture()
def client(monkeypatch):
    import asyncio
    from app import main
    asyncio.get_event_loop_policy().get_event_loop().run_until_complete(init_db()) if False else None
    monkeypatch.setattr(uc, "client_factory", lambda s, a, h: FakeClient(s, a, h))
    monkeypatch.setattr(main.settings, "webhook_secret", "test-webhook-secret-xyz")
    FakeClient.two_fa, FakeClient.authorized = False, True
    uc._pending.clear()
    with TestClient(main.app) as c:
        yield c
        c.post("/api/account/disconnect", json={"forget_api": True}, headers=h(OWNER))


def h(uid):
    return {"X-Telegram-Init-Data": make_init_data(uid)}


API = {"api_id": "123456", "api_hash": "0123456789abcdef0123456789abcdef"}


def test_non_owner_is_blocked_everywhere(client):
    for path in ["/api/account/status"]:
        r = client.get(path, headers=h(STRANGER))
        assert r.status_code == 403 and "owner_only:42" in r.json()["detail"]
    for path in ["/api/account/send-code", "/api/account/code", "/api/account/password",
                 "/api/account/disconnect", "/api/account/api-credentials", "/api/account/verify"]:
        assert client.post(path, json={}, headers=h(STRANGER)).status_code in (403, 422)


def test_unauthenticated_rejected(client):
    assert client.get("/api/account/status").status_code == 401


def test_api_credentials_validated_and_encrypted(client):
    r = client.post("/api/account/api-credentials", json={"api_id": "abc", "api_hash": "x"}, headers=h(OWNER))
    assert r.status_code == 400 and r.json()["code"] == "api_invalid"
    r = client.post("/api/account/api-credentials", json=API, headers=h(OWNER))
    assert r.status_code == 200 and r.json()["api_ready"]
    import asyncio
    from app.db import SessionLocal

    async def raw():
        async with SessionLocal() as s:
            return await s.get(uc.TgAccount, OWNER)
    acc = asyncio.get_event_loop().run_until_complete(raw()) if False else None


def _login(client, code="73914"):
    client.post("/api/account/api-credentials", json=API, headers=h(OWNER))
    r = client.post("/api/account/send-code", json={"phone": "+880 1712-345678"}, headers=h(OWNER))
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "code" and "345678" not in r.json()["phone"]
    return client.post("/api/account/code", json={"code": code}, headers=h(OWNER))


def test_full_login_status_verify_disconnect(client):
    r = _login(client)
    assert r.status_code == 200 and r.json()["connected"] and r.json()["username"] == "tuhin"
    st = client.get("/api/account/status", headers=h(OWNER)).json()
    assert st["connected"] and st["account"]["name"] == "Tuhin"
    assert "SESSION-STRING-SECRET" not in str(st)
    v = client.post("/api/account/verify", headers=h(OWNER))
    assert v.status_code == 200 and v.json()["ok"]
    d = client.post("/api/account/disconnect", json={}, headers=h(OWNER)).json()
    assert d["connected"] is False and d["logged_out_at_telegram"] is True
    assert client.get("/api/account/status", headers=h(OWNER)).json()["connected"] is False


def test_wrong_code_then_right_code(client):
    r = _login(client, code="11111")
    assert r.status_code == 400 and r.json()["code"] == "code_invalid"
    r = client.post("/api/account/code", json={"code": "73914"}, headers=h(OWNER))
    assert r.status_code == 200 and r.json()["connected"]


def test_two_step_verification(client):
    FakeClient.two_fa = True
    r = _login(client)
    assert r.json() == {"stage": "password"}
    bad = client.post("/api/account/password", json={"password": "nope"}, headers=h(OWNER))
    assert bad.status_code == 400 and bad.json()["code"] == "password_invalid"
    ok = client.post("/api/account/password", json={"password": "hunter2"}, headers=h(OWNER))
    assert ok.status_code == 200 and ok.json()["connected"]


def test_invalid_phone(client):
    client.post("/api/account/api-credentials", json=API, headers=h(OWNER))
    r = client.post("/api/account/send-code", json={"phone": "12"}, headers=h(OWNER))
    assert r.json()["code"] == "phone_invalid"
    r = client.post("/api/account/send-code", json={"phone": "+10000000000"}, headers=h(OWNER))
    assert r.json()["code"] == "phone_invalid"


def test_code_without_pending_login(client):
    r = client.post("/api/account/code", json={"code": "73914"}, headers=h(OWNER))
    assert r.status_code == 400 and r.json()["code"] == "no_pending"


def test_revoked_session_detected(client):
    _login(client)
    FakeClient.authorized = False
    r = client.post("/api/account/verify", headers=h(OWNER))
    assert r.json()["code"] == "session_revoked"
    assert client.get("/api/account/status", headers=h(OWNER)).json()["connected"] is False


def test_encryption_roundtrip_and_key_change(monkeypatch):
    from app import main
    monkeypatch.setattr(main.settings, "webhook_secret", "key-one")
    tok = uc.encrypt("hello")
    assert "hello" not in tok and uc.decrypt(tok) == "hello"
    monkeypatch.setattr(main.settings, "webhook_secret", "key-two")
    with pytest.raises(uc.LoginError):
        uc.decrypt(tok)


def test_secrets_never_logged(client, caplog):
    FakeClient.two_fa = True
    caplog.set_level("DEBUG")
    _login(client)
    client.post("/api/account/password", json={"password": "hunter2"}, headers=h(OWNER))
    text = caplog.text
    for secret in ["hunter2", "73914", "SESSION-STRING-SECRET", API["api_hash"], "1712345678"]:
        assert secret not in text
