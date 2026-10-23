"""Bot controls for full automation mode + the buttons under lead notifications. Admin-only.

Every handler re-checks from_user.id against ADMIN_IDS / ADMIN_TELEGRAM_IDS; lead buttons also check that the
lead belongs to the user who pressed it. "Generate draft" only creates a draft: nothing is ever sent from here.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import accounts as acc
from ..db import SessionLocal
from ..outreach import agent as A
from ..outreach import automation as AU
from ..outreach.models import Lead

log = logging.getLogger(__name__)
router = Router(name="automation")
NOT_ADMIN = "This is admin-only."


def _admin(user) -> bool:
    return bool(user) and getattr(user, "is_bot", False) is not True and acc.is_admin_id(user.id)


async def _status_message(m: Message):
    uid = m.from_user.id
    async with SessionLocal() as s:
        a = await AU.get_auto(s, uid)
        await s.commit()
        text, kb = AU.status_text(a), AU.control_keyboard(AU.state_name(a))
    sent = await m.answer(text, reply_markup=kb, disable_web_page_preview=True)
    async with SessionLocal() as s:                      # this becomes the message the reminder keeps editing
        a = await AU.get_auto(s, uid)
        a.status_message_id = sent.message_id
        a.notify_error = ""
        await s.commit()


@router.message(Command("auto_start", "auto_pause", "auto_stop", "auto_status", "auto_run"))
async def cmd_auto(m: Message):
    if not _admin(m.from_user):
        await m.answer(NOT_ADMIN)
        return
    cmd = (m.text or "").split()[0].split("@")[0].lstrip("/")
    action = cmd.replace("auto_", "")
    uid = m.from_user.id
    if action in ("start", "pause", "stop"):
        await AU.set_mode(uid, action, by="bot")
    elif action == "run":
        ok, msg = await AU.trigger_run(uid, "bot")
        await m.answer(("🔄 " if ok else "ℹ️ ") + escape(msg))
    await _status_message(m)


@router.callback_query(F.data.startswith("au|"))
async def cb_auto(c: CallbackQuery):
    if not _admin(c.from_user):
        await c.answer(NOT_ADMIN, show_alert=True)
        return
    action = (c.data or "").split("|", 1)[1]
    uid = c.from_user.id
    note = ""
    if action in ("start", "pause", "stop"):
        st = await AU.set_mode(uid, action, by="bot")
        note = {"running": "Automation running ✅", "paused": "Paused ⏸", "stopped": "Stopped ⏹"}[st["state"]]
    elif action == "run":
        ok, msg = await AU.trigger_run(uid, "bot")
        note = msg
    else:
        await c.answer("Unknown action")
        return
    if c.message is not None:                            # keep editing the message the button sits under
        async with SessionLocal() as s:
            a = await AU.get_auto(s, uid)
            a.status_message_id = c.message.message_id
            await s.commit()
    await c.answer(note)
    if action == "run":
        await asyncio.sleep(0.3)
    try:
        await AU.send_reminder(uid)
    except Exception as e:
        log.info("status refresh failed: %s", type(e).__name__)


@router.callback_query(F.data.startswith("an|"))
async def cb_lead(c: CallbackQuery):
    if not _admin(c.from_user):
        await c.answer(NOT_ADMIN, show_alert=True)
        return
    try:
        _, action, lead_id = (c.data or "").split("|")
        lead_id = int(lead_id)
    except ValueError:
        await c.answer("Bad button")
        return
    uid = c.from_user.id
    async with SessionLocal() as s:
        lead = await s.get(Lead, lead_id)
        if not lead or lead.owner_id != uid:
            await c.answer("Lead not found.", show_alert=True)
            return
        if action == "s":
            lead.status = "saved"
            await A.add_log(uid, "auto", f"Saved lead #{lead.id} from a bot notification", s=s)
            await s.commit()
            await c.answer("Saved to your tracker 💾")
            return
        if action == "k":
            lead.status = "skipped"
            await A.add_log(uid, "auto", f"Skipped lead #{lead.id} from a bot notification", s=s)
            await s.commit()
            await c.answer("Skipped 🙈")
            if c.message is not None:
                try:
                    await c.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return
        if action == "d":
            if lead.scam_risk == "high":
                await c.answer("High scam risk: drafting is disabled for this lead.", show_alert=True)
                return
            d = await A.create_draft(s, uid, lead)
            await s.commit()
            title = escape(lead.project or lead.title or "this lead")
            to = f"{escape(d.channel)}: {escape(d.recipient)}" if d.recipient else "no public contact (apply manually)"
            note = "" if d.status == "pending" else f"\n⚠️ {escape(d.error or d.status)}"
            await c.answer("Draft created ✍️")
            if c.message is not None:
                await c.message.answer(
                    f"✍️ <b>Draft for {title}</b> → {to}{note}\n\n{escape(d.text)[:3000]}\n\n"
                    "<i>Not sent. Review, edit and approve it in the Mini App → Outreach.</i>",
                    disable_web_page_preview=True)
            return
    await c.answer("Unknown action")
