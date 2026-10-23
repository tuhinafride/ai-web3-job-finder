"""Full automation mode (admins only): a backend scheduler that searches on its own and notifies via the BOT.

- State lives in the database (automation_state), so it survives restarts and resumes by itself.
- A single asyncio loop (started in the FastAPI lifespan) wakes every TICK_SECONDS, starts due search runs and
  sends the status reminder. It runs whether or not the Mini App is open.
- New matching leads (relevance >= setting, scam risk not high) are announced by the bot to the admin's private
  chat. lead_notifications has a UNIQUE(owner_id, lead_key) key, so the same lead is never announced twice.
- Telegram limits: at most ~1 message per second per chat, more than DIGEST_THRESHOLD new leads become one digest,
  RetryAfter is honoured, Forbidden (admin never pressed Start) is recorded and shown, not retried in a loop.
- Outreach to recruiters is NEVER sent automatically. "Generate draft" only creates a draft that still needs
  approval in the Mini App.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import SessionLocal, utcnow
from . import agent as A
from . import search as S
from .models import ROLE_LABELS, AgentState, AutomationState, Lead, LeadNotification, OutreachLog

log = logging.getLogger("automation")

TICK_SECONDS = 15
MIN_SEARCH_INTERVAL, MAX_SEARCH_INTERVAL = 5, 1440
MIN_REMINDER_INTERVAL, MAX_REMINDER_INTERVAL = 1, 1440
APP_OPEN_DEBOUNCE = timedelta(minutes=5)
DIGEST_THRESHOLD = 5
PER_CHAT_GAP = 1.05            # seconds between bot messages to the same chat
VALID_SOURCES = ("google", "x", "telegram")

_bot = None
_tasks: dict[int, asyncio.Task] = {}
_last_send: dict[int, float] = {}
_chat_locks: dict[int, asyncio.Lock] = {}
_loop_task: asyncio.Task | None = None


def set_bot(bot):
    global _bot
    _bot = bot


def get_bot():
    return _bot


def is_admin(uid: int | None) -> bool:
    from ..accounts import is_admin_id
    return is_admin_id(uid)


# ------------------------------------------------------------------ state
def state_name(a: AutomationState) -> str:
    if not a.enabled:
        return "stopped"
    return "paused" if a.paused else "running"


async def get_auto(s, owner_id: int) -> AutomationState:
    a = await s.get(AutomationState, owner_id)
    if a is None:
        a = AutomationState(owner_id=owner_id, enabled=False, paused=False, search_interval_minutes=15,
                            reminder_interval_minutes=1, sources=",".join(VALID_SOURCES), min_relevance=50,
                            last_status="idle", last_error="", notify_error="", runs_total=0, runs_failed=0,
                            leads_found_total=0, leads_notified_total=0, last_new_leads=0, last_notified=0,
                            consecutive_failures=0, last_trigger="")
        s.add(a)
        await s.flush()
    return a


def sources_of(a: AutomationState) -> list[str]:
    return [x for x in A.csv(a.sources) if x in VALID_SOURCES] or ["google", "x"]


def run_in_progress(owner_id: int) -> bool:
    t = _tasks.get(owner_id)
    return bool(t and not t.done()) or A.discovery_running(owner_id)


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def auto_dict(a: AutomationState) -> dict:
    return {
        "state": state_name(a), "enabled": a.enabled, "paused": a.paused,
        "running_now": run_in_progress(a.owner_id),
        "search_interval_minutes": a.search_interval_minutes,
        "reminder_interval_minutes": a.reminder_interval_minutes,
        "sources": sources_of(a), "min_relevance": a.min_relevance,
        "last_run_at": iso(a.last_run_at), "last_run_started_at": iso(a.last_run_started_at),
        "next_run_at": iso(a.next_run_at) if a.enabled and not a.paused else None,
        "last_status": a.last_status, "last_error": a.last_error, "last_trigger": a.last_trigger,
        "counters": {"runs_total": a.runs_total, "runs_failed": a.runs_failed,
                     "leads_found_total": a.leads_found_total, "leads_notified_total": a.leads_notified_total,
                     "last_new_leads": a.last_new_leads, "last_notified": a.last_notified,
                     "consecutive_failures": a.consecutive_failures},
        "last_reminder_at": iso(a.last_reminder_at), "notify_error": a.notify_error,
        "provider_backoff": S.backoff.snapshot(),
        "limits": {"search_interval_min": MIN_SEARCH_INTERVAL, "reminder_interval_min": MIN_REMINDER_INTERVAL},
    }


async def status(owner_id: int) -> dict:
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        await s.commit()
        return auto_dict(a)


async def update_settings(owner_id: int, *, search_interval_minutes: int | None = None,
                          reminder_interval_minutes: int | None = None, sources: list[str] | None = None,
                          min_relevance: int | None = None) -> dict:
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        if search_interval_minutes is not None:
            a.search_interval_minutes = max(MIN_SEARCH_INTERVAL, min(MAX_SEARCH_INTERVAL, int(search_interval_minutes)))
            if a.enabled and not a.paused:
                base = a.last_run_at or utcnow()
                a.next_run_at = max(utcnow(), base + timedelta(minutes=a.search_interval_minutes))
        if reminder_interval_minutes is not None:
            a.reminder_interval_minutes = max(MIN_REMINDER_INTERVAL, min(MAX_REMINDER_INTERVAL, int(reminder_interval_minutes)))
        if sources is not None:
            clean = [x for x in dict.fromkeys(sources) if x in VALID_SOURCES]
            if not clean:
                raise ValueError("Pick at least one source (google, x, telegram).")
            a.sources = ",".join(clean)
        if min_relevance is not None:
            a.min_relevance = max(0, min(100, int(min_relevance)))
        await A.add_log(owner_id, "auto", f"Automation settings: every {a.search_interval_minutes} min, reminder every "
                        f"{a.reminder_interval_minutes} min, sources {a.sources}, min relevance {a.min_relevance}%", s=s)
        await s.commit()
        return auto_dict(a)


async def set_mode(owner_id: int, action: str, by: str = "app") -> dict:
    """action: start | pause | stop. Persisted immediately."""
    if action not in ("start", "pause", "stop", "resume"):
        raise ValueError("Unknown action")
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        now = utcnow()
        if action in ("start", "resume"):
            a.enabled, a.paused = True, False
            a.next_run_at = now                       # first run on the next tick
            a.consecutive_failures = 0
            ag = await A.get_state(s, owner_id)
            if ag.stopped:                            # Start is an explicit "go": lift the agent kill switch too
                ag.stopped = False
        elif action == "pause":
            a.enabled, a.paused = True, True
        else:
            a.enabled, a.paused, a.next_run_at = False, False, None
        await A.add_log(owner_id, "auto", f"Automation {state_name(a)} ({action} via {by})", s=s)
        await s.commit()
        out = auto_dict(a)
    if action in ("pause", "stop"):
        t = _tasks.get(owner_id)
        if t and not t.done():
            t.cancel()
        out["running_now"] = False
    return out


# ------------------------------------------------------------------ runs
async def trigger_run(owner_id: int, trigger: str = "manual") -> tuple[bool, str]:
    """Starts a search run in the background. trigger: schedule | app_open | manual | bot."""
    if not is_admin(owner_id):
        return False, "Automation is admin-only."
    if run_in_progress(owner_id):
        return False, "A search is already running."
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        await s.commit()
        if trigger == "app_open":
            if not a.enabled or a.paused:
                return False, "Automation is not running."
            last = a.last_run_started_at or a.last_run_at
            if last and utcnow() - last < APP_OPEN_DEBOUNCE:
                return False, "Searched less than 5 minutes ago."
    task = asyncio.create_task(run_once(owner_id, trigger))
    _tasks[owner_id] = task
    A._running[owner_id] = task          # dashboard shows "Searching…" and manual searches wait
    return True, "Search started."


async def run_once(owner_id: int, trigger: str = "schedule") -> dict:
    try:
        return await _run_once(owner_id, trigger)
    except asyncio.CancelledError:
        async def mark():
            async with SessionLocal() as s:
                a = await get_auto(s, owner_id)
                a.last_status, a.last_error = "cancelled", "Run cancelled (paused or stopped)."
                await s.commit()
        await asyncio.shield(asyncio.ensure_future(mark()))
        raise


async def _run_once(owner_id: int, trigger: str) -> dict:
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        ag = await A.get_state(s, owner_id)
        now = utcnow()
        if ag.stopped:
            a.last_status, a.last_error = "skipped", "Agent is stopped (Home → Resume Agent)."
            a.next_run_at = now + timedelta(minutes=a.search_interval_minutes)
            await s.commit()
            return {"skipped": True}
        a.last_status, a.last_run_started_at, a.last_trigger = "running", now, trigger
        sources = sources_of(a)
        if not is_admin(owner_id):
            sources = [x for x in sources if x != "telegram"]
        min_rel, interval = a.min_relevance, a.search_interval_minutes
        await s.commit()

    summary: dict = {"errors": [], "new_ids": [], "by_source": {}}
    status_, err_text = "ok", ""
    try:
        summary = await A.run_discovery(owner_id, sources, trigger="auto")
    except Exception as e:
        status_, err_text = "error", f"{type(e).__name__}: {e}"[:500]
        log.warning("automation run failed for %s: %s", owner_id, err_text)

    errors = summary.get("errors") or []
    if status_ == "ok" and errors:
        status_ = "error" if not sum((summary.get("by_source") or {}).values()) else "partial"
        err_text = "; ".join(errors)[:1000]

    notified, notify_err = 0, ""
    if summary.get("new_ids"):
        try:
            notified, notify_err = await notify_leads(owner_id, summary["new_ids"], min_rel)
        except Exception as e:
            notify_err = f"notify failed: {type(e).__name__}"
            log.warning("notify failed: %s", type(e).__name__)

    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        now = utcnow()
        a.last_run_at = now
        a.last_status, a.last_error = status_, err_text
        a.runs_total += 1
        if status_ == "error":
            a.runs_failed += 1
            a.consecutive_failures += 1
        else:
            a.consecutive_failures = 0
        a.last_new_leads = len(summary.get("new_ids") or [])
        a.leads_found_total += a.last_new_leads
        a.last_notified = notified
        a.leads_notified_total += notified
        if notify_err:
            a.notify_error = notify_err[:300]
        elif notified:
            a.notify_error = ""
        a.next_run_at = now + timedelta(minutes=interval) if a.enabled and not a.paused else None
        s.add(OutreachLog(owner_id=owner_id, kind="auto", level="info" if status_ == "ok" else "warn",
                          message=(f"Automation run ({trigger}): {status_}, {a.last_new_leads} new lead(s), "
                                   f"{notified} sent to you by the bot" + (f"; {err_text}" if err_text else "")
                                   + (f"; notify: {notify_err}" if notify_err else ""))[:1000]))
        await s.commit()
    return {"status": status_, "new": len(summary.get("new_ids") or []), "notified": notified,
            "errors": errors, "notify_error": notify_err}


# ------------------------------------------------------------------ bot messages
def _kb(rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**b) for b in r] for r in rows if r])


def lead_keyboard(lead_id: int):
    return _kb([[{"text": "💾 Save", "callback_data": f"an|s|{lead_id}"},
                 {"text": "✍️ Generate draft", "callback_data": f"an|d|{lead_id}"},
                 {"text": "🙈 Skip", "callback_data": f"an|k|{lead_id}"}]])


def control_keyboard(state: str):
    row = [{"text": "▶️ Start", "callback_data": "au|start"}, {"text": "⏸ Pause", "callback_data": "au|pause"},
           {"text": "⏹ Stop", "callback_data": "au|stop"}, {"text": "🔄 Run now", "callback_data": "au|run"}]
    if state == "running":
        row = row[1:]
    elif state == "paused":
        row = [row[0]] + row[2:]
    else:
        row = [row[0], row[3]]
    url = get_settings().webapp_url
    app = [{"text": "📱 Open dashboard", "web_app": {"url": url}}] if url.startswith("https://") else []
    return _kb([row, app])


def _pay(l: Lead) -> str:
    if l.pay_detail:
        return l.pay_detail
    return {"paid": "Paid (amount not stated)", "unpaid": "Unpaid", "unknown": "Pay not stated"}.get(l.pay_status, "")


def lead_text(l: Lead) -> str:
    src = {"google": "🔎 Google", "x": "𝕏 X", "telegram": "✈️ Telegram", "board": "🏢 Board"}.get(l.source, l.source)
    lines = [f"🆕 <b>{escape(l.title or 'Web3 opportunity')[:200]}</b>"]
    proj = escape(l.project or "Unknown project")[:120]
    lines.append(f"🏢 {proj} · {escape(ROLE_LABELS.get(l.role, 'Other'))}")
    lines.append(f"💰 {escape(_pay(l))[:120]}" + (" · 🌍 Remote" if l.remote == "yes" else ""))
    lines.append(f"⭐ {l.relevance}% match · {src}" + ("" if l.source_checked else " · source not verified"))
    flags = [x for x in (l.scam_flags or "").split("\n") if x]
    if flags or l.scam_risk != "low":
        lines.append(f"⚠️ Scam risk {l.scam_risk}: " + escape("; ".join(flags[:3]) or "check carefully")[:300])
    if l.source_url.startswith("http"):
        lines.append(f'🔗 <a href="{escape(l.source_url, quote=True)}">Open source</a>')
    lines.append("<i>Nothing is sent to anyone without your approval.</i>")
    return "\n".join(lines)[:3900]


def digest_text(leads: list[Lead]) -> str:
    lines = [f"🆕 <b>{len(leads)} new Web3 leads</b> (best first)"]
    for i, l in enumerate(leads[:15], 1):
        title = escape((l.title or "Web3 opportunity")[:80])
        link = f'<a href="{escape(l.source_url, quote=True)}">{title}</a>' if l.source_url.startswith("http") else title
        warn = " ⚠️" if l.scam_risk != "low" else ""
        lines.append(f"{i}. {link} · {escape((l.project or '?')[:40])} · {escape(_pay(l)[:40])} · {l.relevance}%{warn}")
    if len(leads) > 15:
        lines.append(f"…and {len(leads) - 15} more in the Mini App.")
    lines.append("Open the Mini App → Jobs to save, draft or skip. Nothing is sent without your approval.")
    return "\n".join(lines)[:3900]


class NotifyBlocked(Exception):
    """The bot can't message this chat (user never pressed Start, blocked the bot, chat not found)."""


async def bot_send(chat_id: int, text: str, reply_markup=None) -> int:
    """Sends one bot message with per-chat pacing and RetryAfter handling. Returns the message id."""
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    if _bot is None:
        raise NotifyBlocked("Bot token not configured.")
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        for attempt in range(3):
            wait = _last_send.get(chat_id, 0) + PER_CHAT_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                msg = await _bot.send_message(chat_id, text, reply_markup=reply_markup,
                                              disable_web_page_preview=True)
                _last_send[chat_id] = time.monotonic()
                return msg.message_id
            except TelegramRetryAfter as e:
                _last_send[chat_id] = time.monotonic()
                if attempt == 2:
                    raise
                await asyncio.sleep(min(60, int(getattr(e, "retry_after", 5)) + 1))
            except TelegramForbiddenError:
                raise NotifyBlocked("The bot can't message you. Open the bot and press Start (or unblock it).")
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    raise NotifyBlocked("Chat not found. Open the bot and press Start.")
                raise
    raise RuntimeError("send failed")


async def _already_notified(s, owner_id: int, lead: Lead) -> bool:
    cond = [LeadNotification.lead_key == lead.uid]
    if lead.fp:
        cond.append(LeadNotification.text_fp == lead.fp)
    return bool(await s.scalar(select(LeadNotification.id).where(
        LeadNotification.owner_id == owner_id, or_(*cond)).limit(1)))


async def notify_leads(owner_id: int, lead_ids: list[int], min_relevance: int = 50) -> tuple[int, str]:
    """Announces new matching leads through the bot. Returns (sent_count, error_text)."""
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Lead).where(Lead.owner_id == owner_id, Lead.id.in_(lead_ids))
                                .order_by(Lead.relevance.desc(), Lead.id))).all()
        todo: list[Lead] = []
        for l in rows:
            if l.relevance < min_relevance or l.scam_risk == "high" or l.status == "skipped":
                continue
            if await _already_notified(s, owner_id, l):
                continue
            # reserve first: the unique key makes a second announcement impossible, even across processes
            try:
                async with s.begin_nested():
                    s.add(LeadNotification(owner_id=owner_id, lead_key=l.uid, text_fp=l.fp or "", lead_id=l.id))
                todo.append(l)
            except IntegrityError:
                continue
        await s.commit()
    if not todo:
        return 0, ""

    async def release(leads: list[Lead]):
        async with SessionLocal() as s:
            await s.execute(delete(LeadNotification).where(LeadNotification.owner_id == owner_id,
                                                           LeadNotification.lead_key.in_([l.uid for l in leads])))
            await s.commit()

    async def mark(leads: list[Lead], message_id: int, digest: bool):
        async with SessionLocal() as s:
            for n in (await s.scalars(select(LeadNotification).where(
                    LeadNotification.owner_id == owner_id,
                    LeadNotification.lead_key.in_([l.uid for l in leads])))).all():
                n.message_id, n.digest = message_id, digest
            await s.commit()

    sent = 0
    try:
        if len(todo) > DIGEST_THRESHOLD:
            url = get_settings().webapp_url
            kb = _kb([[{"text": "📱 Open leads", "web_app": {"url": url}}]]) if url.startswith("https://") else None
            mid = await bot_send(owner_id, digest_text(todo), kb)
            await mark(todo, mid, True)
            sent = len(todo)
        else:
            for i, l in enumerate(todo):
                mid = await bot_send(owner_id, lead_text(l), lead_keyboard(l.id))
                await mark([l], mid, False)
                sent += 1
    except NotifyBlocked as e:
        await release(todo[sent:] if len(todo) <= DIGEST_THRESHOLD else todo)   # never sent: may be sent later
        await A.add_log(owner_id, "auto", f"Bot notification failed: {e}", "error")
        return (sent if len(todo) <= DIGEST_THRESHOLD else 0), str(e)
    except Exception as e:
        from aiogram.exceptions import TelegramNetworkError
        if not isinstance(e, TelegramNetworkError):          # a clear API refusal: not delivered
            await release(todo[sent:] if len(todo) <= DIGEST_THRESHOLD else todo)
        # network errors: delivery unknown -> keep the reservation (never risk a duplicate)
        await A.add_log(owner_id, "auto", f"Bot notification error: {type(e).__name__}", "error")
        return sent, f"Telegram error: {type(e).__name__}"
    return sent, ""


# ------------------------------------------------------------------ status reminder
def _fmt(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "—"
    secs = int((dt - now).total_seconds())
    rel = (f"in {secs // 60} min" if secs >= 60 else "in <1 min") if secs >= 0 else \
          (f"{-secs // 60} min ago" if -secs >= 60 else "just now")
    return f"{dt.strftime('%H:%M')} UTC ({rel})"


def status_text(a: AutomationState, now: datetime | None = None) -> str:
    now = now or utcnow()
    st = state_name(a)
    icon = {"running": "🟢 Running", "paused": "⏸ Paused", "stopped": "⏹ Stopped"}[st]
    lines = ["🤖 <b>Job agent automation</b>", f"State: {icon}" + (" · 🔄 searching now…" if run_in_progress(a.owner_id) else "")]
    lines.append(f"Last run: {_fmt(a.last_run_at, now)}" + (f" · {escape(a.last_status)}" if a.last_run_at else ""))
    if st == "running":
        lines.append(f"Next run: {_fmt(a.next_run_at, now)} · every {a.search_interval_minutes} min")
    lines.append(f"New leads last run: {a.last_new_leads} · sent to you: {a.last_notified}")
    lines.append(f"Totals: {a.runs_total} runs · {a.leads_found_total} leads · {a.leads_notified_total} notified")
    lines.append(f"Sources: {', '.join(sources_of(a))} · min match {a.min_relevance}%")
    if a.last_error:
        lines.append("⚠️ Last errors: " + escape(a.last_error[:500]))
    if a.notify_error:
        lines.append("⚠️ Notifications: " + escape(a.notify_error[:200]))
    lines.append(f"<i>Updated {now.strftime('%H:%M:%S')} UTC · outreach messages always need your approval</i>")
    return "\n".join(lines)[:3900]


async def send_reminder(owner_id: int, force_new: bool = False) -> str:
    """Edits the remembered status message; sends (and pins) a new one only if editing is impossible.
    Returns 'edited' | 'sent' | 'skipped' | 'blocked'."""
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        text, kb, mid = status_text(a), control_keyboard(state_name(a)), a.status_message_id
        await s.commit()
    if _bot is None:
        return "skipped"
    outcome, new_id, err = "skipped", None, None
    if mid and not force_new:
        try:
            await _bot.edit_message_text(text=text, chat_id=owner_id, message_id=mid, reply_markup=kb,
                                         disable_web_page_preview=True)
            outcome = "edited"
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                outcome = "edited"
        except TelegramRetryAfter:
            return "skipped"
        except TelegramForbiddenError:
            err = "The bot can't message you. Open the bot and press Start (or unblock it)."
            outcome = "blocked"
        except Exception as e:
            log.info("status edit failed: %s", type(e).__name__)
    if outcome == "skipped":
        try:
            new_id = await bot_send(owner_id, text, kb)
            outcome = "sent"
            try:
                await _bot.pin_chat_message(chat_id=owner_id, message_id=new_id, disable_notification=True)
            except Exception:
                pass
        except NotifyBlocked as e:
            err, outcome = str(e), "blocked"
        except Exception as e:
            log.info("status send failed: %s", type(e).__name__)
            return "skipped"
    async with SessionLocal() as s:
        a = await get_auto(s, owner_id)
        a.last_reminder_at = utcnow()
        if new_id:
            a.status_message_id = new_id
        if err:
            a.notify_error = err[:300]
        elif outcome in ("edited", "sent") and a.notify_error.startswith(("The bot can't", "Chat not found")):
            a.notify_error = ""
        await s.commit()
    return outcome


# ------------------------------------------------------------------ scheduler
async def tick(now: datetime | None = None) -> dict:
    """One scheduler pass: start due runs, send due reminders. Safe to call often."""
    now = now or utcnow()
    started, reminded = [], []
    async with SessionLocal() as s:
        rows = (await s.scalars(select(AutomationState).where(AutomationState.enabled.is_(True)))).all()
    for a in rows:
        if not is_admin(a.owner_id) or a.paused:
            continue
        if (a.next_run_at is None or now >= a.next_run_at) and not run_in_progress(a.owner_id):
            ok, _ = await trigger_run(a.owner_id, "schedule")
            if ok:
                started.append(a.owner_id)
        due = a.last_reminder_at is None or now - a.last_reminder_at >= timedelta(minutes=a.reminder_interval_minutes)
        if due:
            await send_reminder(a.owner_id)
            reminded.append(a.owner_id)
    return {"started": started, "reminded": reminded}


async def on_startup() -> list[int]:
    """Resume after a restart: enabled automations continue; a run cut off by the restart is marked."""
    resumed = []
    async with SessionLocal() as s:
        rows = (await s.scalars(select(AutomationState).where(AutomationState.enabled.is_(True)))).all()
        for a in rows:
            if a.last_status == "running":
                a.last_status, a.last_error = "interrupted", "The server restarted during a run."
            if not a.paused:
                if a.next_run_at is None:
                    a.next_run_at = utcnow()
                resumed.append(a.owner_id)
                s.add(OutreachLog(owner_id=a.owner_id, kind="auto", message="Automation resumed after a server restart"))
        await s.commit()
    if resumed:
        log.info("automation resumed for %d admin(s)", len(resumed))
    return resumed


async def scheduler_loop():
    await asyncio.sleep(5)
    try:
        await on_startup()
    except Exception:
        log.exception("automation startup failed")
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("automation tick failed")
        await asyncio.sleep(TICK_SECONDS)


def start_scheduler() -> asyncio.Task:
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(scheduler_loop())
    return _loop_task


async def logs(owner_id: int, limit: int = 80) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.scalars(select(OutreachLog).where(
            OutreachLog.owner_id == owner_id,
            or_(OutreachLog.kind == "auto", OutreachLog.level.in_(["warn", "error"])))
            .order_by(OutreachLog.id.desc()).limit(limit))).all()
    return [{"ts": iso(l.ts), "kind": l.kind, "level": l.level, "message": l.message} for l in rows]
