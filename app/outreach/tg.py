"""Telegram user-account actions for outreach (uses the owner's connected session).

Separate from the bot: the bot token is never used here, and this never sends as the bot.
"""
from __future__ import annotations

import logging

from .. import userclient as uc
from ..db import SessionLocal

log = logging.getLogger("outreach.tg")


class SendBlocked(Exception):
    def __init__(self, code: str, message: str, stop_agent: bool = False):
        super().__init__(message)
        self.code, self.message, self.stop_agent = code, message, stop_agent


async def open_client(owner_id: int):
    api_id, api_hash, _ = await uc.api_credentials(owner_id)
    async with SessionLocal() as s:
        acc = await s.get(uc.TgAccount, owner_id)
        if not acc or not acc.session_enc:
            raise SendBlocked("not_connected", "Connect your Telegram account first (Settings → Telegram account).")
        sess = uc.decrypt(acc.session_enc)
    client = uc.client_factory(sess, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SendBlocked("session_revoked", "Telegram no longer accepts the saved session. Log in again.")
    return client


async def send_dm(owner_id: int, username: str, text: str) -> int:
    """Sends one private message to a public @username. Returns the message id."""
    client = await open_client(owner_id)
    try:
        try:
            ent = await client.get_entity(username)
        except Exception as e:
            raise SendBlocked("recipient_not_found", f"@{username} could not be found on Telegram ({type(e).__name__}).")
        kind = type(ent).__name__
        if kind != "User":
            raise SendBlocked("not_a_person", f"@{username} is a {kind.lower()}, not a person. Contact them another way.")
        if getattr(ent, "bot", False):
            raise SendBlocked("is_bot", f"@{username} is a bot.")
        if getattr(ent, "is_self", False):
            raise SendBlocked("is_self", "That's your own account.")
        try:
            msg = await client.send_message(ent, text, link_preview=False)
        except Exception as e:
            name = type(e).__name__
            if name == "FloodWaitError":
                raise SendBlocked("flood_wait", f"Telegram asked to wait {getattr(e, 'seconds', '?')}s. Agent paused.", True)
            if name == "PeerFloodError":
                raise SendBlocked("peer_flood", "Telegram limited messages to non-contacts on your account. Agent stopped; "
                                  "wait at least a day and check @SpamBot.", True)
            if name in ("UserPrivacyRestrictedError", "PrivacyPremiumRequiredError", "UserIsBlockedError",
                        "YouBlockedUserError", "InputUserDeactivatedError", "ChatWriteForbiddenError"):
                raise SendBlocked("privacy", f"@{username} doesn't accept messages from you ({name}). Use another contact.")
            raise SendBlocked("telegram_error", f"Telegram error: {name}")
        return msg.id
    finally:
        await client.disconnect()
