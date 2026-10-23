"""Personal Telegram account login (MTProto user client via Telethon).

This is NOT the bot. It signs in to the owner's own Telegram account with Telegram's
official login flow: phone number -> login code sent by Telegram -> optional 2FA password.

Security model
- Only Telegram user IDs listed in ADMIN_IDS can use any endpoint (verified Mini App initData).
- The login code and 2FA password are typed by the owner in the Mini App, sent once over HTTPS,
  passed straight to Telegram and never stored or logged.
- The resulting session string is encrypted (Fernet, key from SESSION_ENC_KEY) before it is
  written to the database. Disconnect logs the session out at Telegram and deletes it.
- api_id / api_hash come from env (TG_API_ID / TG_API_HASH) or are entered once by the owner;
  the hash is stored encrypted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import BigInteger, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .db import Base, SessionLocal, utcnow

log = logging.getLogger("userclient")

try:  # imported lazily-safe so the bot still runs if telethon is missing
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover
    TelegramClient = None  # type: ignore
    errors = None  # type: ignore
    StringSession = None  # type: ignore


# ------------------------------------------------------------------ storage
class TgAccount(Base):
    """One connected personal account per owner."""
    __tablename__ = "tg_accounts"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_id: Mapped[str] = mapped_column(String(32), default="")
    api_hash_enc: Mapped[str] = mapped_column(Text, default="")
    session_enc: Mapped[str] = mapped_column(Text, default="")
    account_name: Mapped[str] = mapped_column(String(200), default="")
    account_username: Mapped[str] = mapped_column(String(64), default="")
    account_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phone_masked: Mapped[str] = mapped_column(String(32), default="")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def _fernet() -> Fernet:
    st = get_settings()
    material = st.session_enc_key or ""
    if not material:
        raise LoginError("server_not_configured",
                         "SESSION_ENC_KEY is not set on the server, so the session can't be stored safely.")
    key = base64.urlsafe_b64encode(hashlib.sha256(("tg-session:" + material).encode()).digest())
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise LoginError("bad_key", "Stored session can't be decrypted (SESSION_ENC_KEY changed). Log in again.")


def mask_phone(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    return f"+{d[:3]}•••••{d[-2:]}" if len(d) >= 6 else "+•••"


def normalise_phone(phone: str) -> str:
    p = re.sub(r"[\s\-().]", "", phone or "")
    if p.startswith("00"):
        p = "+" + p[2:]
    if not re.fullmatch(r"\+\d{7,15}", p):
        raise LoginError("phone_invalid", "Enter the number in international format, e.g. +8801XXXXXXXXX.")
    return p


# ------------------------------------------------------------------ errors
class LoginError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _translate(e: Exception) -> LoginError:
    """Map Telethon errors to safe, user-facing messages (never echo secrets)."""
    name = type(e).__name__
    if errors is not None and isinstance(e, errors.FloodWaitError):
        return LoginError("flood_wait", f"Telegram asks you to wait {e.seconds} seconds before trying again.", 429)
    table = {
        "PhoneNumberInvalidError": ("phone_invalid", "Telegram says this phone number is invalid."),
        "PhoneNumberBannedError": ("phone_banned", "Telegram reports this number is banned."),
        "PhoneNumberFloodError": ("flood_wait", "Too many login attempts. Wait a while and try again."),
        "PhoneCodeInvalidError": ("code_invalid", "That code is wrong. Check the latest code from Telegram."),
        "PhoneCodeExpiredError": ("code_expired", "That code expired. Request a new one."),
        "PhoneCodeEmptyError": ("code_invalid", "Enter the code Telegram sent you."),
        "PasswordHashInvalidError": ("password_invalid", "Wrong two-step verification password."),
        "ApiIdInvalidError": ("api_invalid", "api_id / api_hash are not valid. Re-check them on my.telegram.org."),
        "ApiIdPublishedFloodError": ("api_invalid", "This api_id is blocked by Telegram. Create your own on my.telegram.org."),
        "AuthRestartError": ("restart", "Telegram asked to restart the login. Request a new code."),
        "SendCodeUnavailableError": ("code_unavailable", "Telegram can't send more codes right now. Try later."),
        "PhoneNumberUnoccupiedError": ("no_account", "No Telegram account uses this number."),
    }
    if name in table:
        c, m = table[name]
        return LoginError(c, m)
    log.warning("telegram login error: %s", name)  # class name only, no payload
    return LoginError("telegram_error", f"Telegram error: {name}.")


# ------------------------------------------------------------------ pending logins
@dataclass
class Pending:
    client: object
    phone: str
    phone_code_hash: str
    api_id: int
    api_hash: str
    stage: str = "code"           # "code" -> "password"
    created: float = field(default_factory=time.time)
    attempts: int = 0


PENDING_TTL = 600          # a started login is dropped after 10 minutes
MAX_ATTEMPTS = 5
_pending: dict[int, Pending] = {}
_lock = asyncio.Lock()


def client_factory(session_str: str, api_id: int, api_hash: str):
    """Builds a Telethon client from a (possibly empty) StringSession. Overridable in tests."""
    if TelegramClient is None:
        raise LoginError("server_not_configured", "telethon is not installed on the server.", 500)
    return TelegramClient(StringSession(session_str or ""), api_id, api_hash, device_model="Web3 Job Finder",
                          system_version="server", app_version="1.0", lang_code="en")


async def _drop_pending(owner_id: int):
    p = _pending.pop(owner_id, None)
    if p:
        try:
            await p.client.disconnect()
        except Exception:
            pass


async def _expire_old():
    now = time.time()
    for oid in [o for o, p in _pending.items() if now - p.created > PENDING_TTL]:
        await _drop_pending(oid)


# ------------------------------------------------------------------ credentials
async def _get_account(s, owner_id: int) -> TgAccount | None:
    return await s.get(TgAccount, owner_id)


async def api_credentials(owner_id: int) -> tuple[int, str, str]:
    """Returns (api_id, api_hash, source). Env vars win over owner-entered values."""
    st = get_settings()
    if st.tg_api_id and st.tg_api_hash:
        return int(st.tg_api_id), st.tg_api_hash, "server"
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id)
        if acc and acc.api_id and acc.api_hash_enc:
            return int(acc.api_id), decrypt(acc.api_hash_enc), "saved"
    raise LoginError("api_missing", "Add your api_id and api_hash from my.telegram.org first.")


async def save_api_credentials(owner_id: int, api_id: str, api_hash: str):
    api_id = (api_id or "").strip()
    api_hash = (api_hash or "").strip().lower()
    if not re.fullmatch(r"\d{3,12}", api_id):
        raise LoginError("api_invalid", "api_id should be a number (from my.telegram.org).")
    if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        raise LoginError("api_invalid", "api_hash should be 32 letters/digits (from my.telegram.org).")
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id) or TgAccount(owner_id=owner_id)
        acc.api_id, acc.api_hash_enc = api_id, encrypt(api_hash)
        s.add(acc)
        await s.commit()


# ------------------------------------------------------------------ flow
async def status(owner_id: int) -> dict:
    await _expire_old()
    st = get_settings()
    out = {"configured": bool(st.session_enc_key), "api_ready": False, "api_source": "",
           "connected": False, "stage": "", "account": None, "verified_live": False}
    try:
        _, _, src = await api_credentials(owner_id)
        out["api_ready"], out["api_source"] = True, src
    except LoginError:
        pass
    p = _pending.get(owner_id)
    if p:
        out["stage"] = p.stage
        out["phone"] = mask_phone(p.phone)
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id)
        if acc and acc.session_enc:
            out["connected"] = True
            out["account"] = {"name": acc.account_name, "username": acc.account_username,
                              "phone": acc.phone_masked,
                              "connected_at": acc.connected_at.isoformat() + "Z" if acc.connected_at else None}
    return out


async def verify_live(owner_id: int) -> dict:
    """Opens the saved session and asks Telegram who we are: proves the login really works."""
    api_id, api_hash, _ = await api_credentials(owner_id)
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id)
        if not acc or not acc.session_enc:
            raise LoginError("not_connected", "No account connected.")
        sess = decrypt(acc.session_enc)
    client = client_factory(sess, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await _clear_session(owner_id)
            raise LoginError("session_revoked", "Telegram no longer accepts this session. Log in again.")
        me = await client.get_me()
        return {"ok": True, "name": _display_name(me), "username": getattr(me, "username", "") or ""}
    except LoginError:
        raise
    except Exception as e:
        raise _translate(e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _display_name(me) -> str:
    return " ".join(x for x in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if x).strip()


async def send_code(owner_id: int, phone: str) -> dict:
    phone = normalise_phone(phone)
    api_id, api_hash, _ = await api_credentials(owner_id)
    _fernet()  # fail early if we can't store the session
    async with _lock:
        await _drop_pending(owner_id)
        client = client_factory("", api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise e if isinstance(e, LoginError) else _translate(e)
        _pending[owner_id] = Pending(client, phone, sent.phone_code_hash, api_id, api_hash)
    kind = type(getattr(sent, "type", None)).__name__.replace("SentCodeType", "").lower() or "app"
    return {"stage": "code", "phone": mask_phone(phone), "delivery": kind}


async def submit_code(owner_id: int, code: str) -> dict:
    await _expire_old()
    p = _pending.get(owner_id)
    if not p or p.stage != "code":
        raise LoginError("no_pending", "Start again: request a new code.")
    code = re.sub(r"\D", "", code or "")
    if not 4 <= len(code) <= 8:
        raise LoginError("code_invalid", "The code is usually 5 digits.")
    p.attempts += 1
    if p.attempts > MAX_ATTEMPTS:
        await _drop_pending(owner_id)
        raise LoginError("too_many", "Too many wrong attempts. Request a new code.")
    try:
        await p.client.sign_in(phone=p.phone, code=code, phone_code_hash=p.phone_code_hash)
    except Exception as e:
        if type(e).__name__ == "SessionPasswordNeededError":
            p.stage, p.attempts = "password", 0
            return {"stage": "password"}
        err = _translate(e)
        if err.code in ("code_expired", "restart"):
            await _drop_pending(owner_id)
        raise err
    return await _finish(owner_id, p)


async def submit_password(owner_id: int, password: str) -> dict:
    await _expire_old()
    p = _pending.get(owner_id)
    if not p or p.stage != "password":
        raise LoginError("no_pending", "Start again: request a new code.")
    if not password or len(password) > 256:
        raise LoginError("password_invalid", "Enter your two-step verification password.")
    p.attempts += 1
    if p.attempts > MAX_ATTEMPTS:
        await _drop_pending(owner_id)
        raise LoginError("too_many", "Too many wrong attempts. Start again later.")
    try:
        await p.client.sign_in(password=password)
    except Exception as e:
        raise _translate(e)
    finally:
        password = ""  # drop our reference immediately
    return await _finish(owner_id, p)


async def _finish(owner_id: int, p: Pending) -> dict:
    me = await p.client.get_me()
    session_str = p.client.session.save()
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id) or TgAccount(owner_id=owner_id)
        acc.session_enc = encrypt(session_str)
        acc.account_name = _display_name(me)[:200]
        acc.account_username = (getattr(me, "username", "") or "")[:64]
        acc.account_tg_id = getattr(me, "id", None)
        acc.phone_masked = mask_phone(p.phone)
        acc.connected_at = utcnow()
        s.add(acc)
        await s.commit()
    _pending.pop(owner_id, None)
    try:
        await p.client.disconnect()
    except Exception:
        pass
    log.info("personal account connected for owner %s", owner_id)
    return {"stage": "done", "connected": True, "name": acc.account_name, "username": acc.account_username}


async def cancel(owner_id: int):
    await _drop_pending(owner_id)


async def _clear_session(owner_id: int):
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id)
        if acc:
            acc.session_enc = ""
            acc.account_name = acc.account_username = acc.phone_masked = ""
            acc.account_tg_id = None
            acc.connected_at = None
            await s.commit()


async def disconnect(owner_id: int, forget_api: bool = False) -> dict:
    """Logs the session out at Telegram (so it disappears from Active Sessions) and deletes it."""
    await _drop_pending(owner_id)
    logged_out = False
    async with SessionLocal() as s:
        acc = await _get_account(s, owner_id)
        sess = decrypt(acc.session_enc) if acc and acc.session_enc else ""
    if sess:
        try:
            api_id, api_hash, _ = await api_credentials(owner_id)
            client = client_factory(sess, api_id, api_hash)
            await client.connect()
            logged_out = bool(await client.log_out())
        except Exception as e:
            log.warning("log_out at Telegram failed: %s", type(e).__name__)
    await _clear_session(owner_id)
    if forget_api:
        async with SessionLocal() as s:
            acc = await _get_account(s, owner_id)
            if acc:
                await s.delete(acc)
                await s.commit()
    return {"connected": False, "logged_out_at_telegram": logged_out}
