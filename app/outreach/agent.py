"""Agent services: discovery runs, lead storage/dedupe, drafts, approval and guarded sending."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta

from sqlalchemy import delete, func, select

from ..db import Job, SessionLocal, utcnow
from . import drafts as drafting
from . import qualify as Q
from . import search as S
from . import tg
from .models import (DEFAULT_PROFILE, generic_profile, DEFAULT_QUERIES, AgentState, ContactedRecipient, Contact, Draft,
                     Lead, OutreachLog, OwnerProfile, ROLE_LABELS)

log = logging.getLogger("outreach")
_running: dict[int, asyncio.Task] = {}
_send_lock = asyncio.Lock()


def csv(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", v or "") if x.strip()]


# ------------------------------------------------------------------ profile / state / logs
def is_admin(owner_id: int) -> bool:
    from ..accounts import is_admin_id
    return is_admin_id(owner_id)


async def get_profile(s, owner_id: int) -> OwnerProfile:
    p = await s.get(OwnerProfile, owner_id)
    if p is None:
        if is_admin(owner_id):
            defaults = dict(DEFAULT_PROFILE)       # the owner's own professional profile
        else:                                   # never give other users the owner's facts
            from ..db import User
            u = await s.get(User, owner_id)
            defaults = generic_profile((u.first_name if u else "") or "")
        p = OwnerProfile(owner_id=owner_id, **defaults)
        s.add(p)
        await s.flush()
    return p


async def get_state(s, owner_id: int) -> AgentState:
    st = await s.get(AgentState, owner_id)
    if st is None:
        st = AgentState(owner_id=owner_id, queries="\n".join(DEFAULT_QUERIES))
        s.add(st)
        await s.flush()
    return st


async def add_log(owner_id: int, kind: str, message: str, level: str = "info", s=None):
    entry = OutreachLog(owner_id=owner_id, kind=kind, message=message[:1000], level=level)
    if s is not None:
        s.add(entry)
        return
    async with SessionLocal() as s2:
        s2.add(entry)
        await s2.commit()


# ------------------------------------------------------------------ storing leads
async def store_lead(s, owner_id: int, raw: S.RawLead, preferred: list[str], use_llm: bool = True,
                     min_relevance: int = 15, llm_budget: dict | None = None) -> tuple[Lead | None, str]:
    """Returns (lead, outcome) where outcome is new | duplicate | irrelevant."""
    uid = Q.lead_uid(raw.url, raw.text)
    fp = Q.text_fingerprint(raw.text)
    from sqlalchemy import or_
    exists = await s.scalar(select(Lead.id).where(Lead.owner_id == owner_id, or_(Lead.uid == uid, Lead.fp == fp) if fp else Lead.uid == uid).limit(1))
    if exists:
        return None, "duplicate"
    q = Q.rule_qualify(raw.text, raw.title, raw.project, raw.posted_at, raw.author_x, raw.author_tg, preferred)
    if not (q.is_hiring or q.role != "other") or (not q.web3 and raw.source != "board"):
        return None, "irrelevant"
    if use_llm and (llm_budget is None or llm_budget.get("left", 0) > 0):
        if llm_budget is not None:
            llm_budget["left"] -= 1
        q = await Q.qualify(raw.text, raw.title, raw.project, raw.posted_at, raw.author_x, raw.author_tg, preferred)
        if q.ai_used and not q.is_hiring:
            return None, "irrelevant"
    if q.relevance < min_relevance and q.scam_risk == "low":
        return None, "irrelevant"
    lead = Lead(owner_id=owner_id, uid=uid, fp=fp, source=raw.source, source_url=raw.url, source_checked=raw.checked,
                posted_at=raw.posted_at, project=q.project or raw.author_name, title=q.title, role=q.role,
                summary=q.summary, responsibilities="\n".join(q.responsibilities),
                requirements="\n".join(q.requirements), pay_status=q.pay_status, pay_detail=q.pay_detail,
                apply_method=q.apply_method, deadline=q.deadline, remote=q.remote, relevance=q.relevance,
                scam_risk=q.scam_risk, scam_flags="\n".join(q.scam_flags), freshness=q.freshness,
                ai_used=q.ai_used, raw_text=raw.text[:8000])
    s.add(lead)
    await s.flush()
    seen = set()
    for c in q.contacts + raw.extra_contacts:
        key = (c["platform"], c["handle"].lower())
        if key in seen:
            continue
        seen.add(key)
        s.add(Contact(lead_id=lead.id, platform=c["platform"], handle=c["handle"][:200],
                      role_hint=c.get("role_hint", "")[:120], found_at_url=raw.url))
    return lead, "new"


async def import_board_job(s, owner_id: int, job: Job, preferred: list[str], use_llm: bool = True) -> Lead:
    uid = Q.lead_uid(job.url)
    lead = await s.scalar(select(Lead).where(Lead.owner_id == owner_id, Lead.uid == uid))
    if lead:
        return lead
    raw = S.RawLead("board", job.url, job.title, f"{job.title} at {job.company}\n{job.salary}\n{job.location}\n"
                    f"{job.requirements}\n{job.description[:4000]}", project=job.company, posted_at=job.posted_at,
                    checked=job.source_type == "company_ats")
    lead, _ = await store_lead(s, owner_id, raw, preferred, use_llm=use_llm, min_relevance=0)
    if lead is None:  # board jobs are always kept when the owner asks
        q = Q.rule_qualify(raw.text, raw.title, raw.project, raw.posted_at, preferred_roles=preferred)
        lead = Lead(owner_id=owner_id, uid=uid, source="board", source_url=job.url, source_checked=raw.checked,
                    posted_at=job.posted_at, project=job.company, title=job.title, role=q.role, summary=q.summary,
                    pay_status=q.pay_status, pay_detail=job.salary or q.pay_detail, apply_method=job.apply_url,
                    remote="yes" if job.remote else "unknown", relevance=q.relevance, scam_risk=q.scam_risk,
                    scam_flags="\n".join(q.scam_flags), freshness=q.freshness, raw_text=raw.text[:8000])
        s.add(lead)
        await s.flush()
    if not lead.apply_method:
        lead.apply_method = job.apply_url
    return lead


# ------------------------------------------------------------------ discovery run
def queries_of(state: AgentState) -> list[str]:
    """Saved search phrases, or the defaults when the owner cleared them all."""
    return csv(state.queries)[:8] or list(DEFAULT_QUERIES)


async def run_discovery(owner_id: int, sources: list[str], custom_query: str = "", trigger: str = "manual") -> dict:
    from . import llm
    from ..config import get_settings
    summary = {"new": 0, "duplicate": 0, "irrelevant": 0, "errors": [], "by_source": {}, "new_ids": [],
               "ai_calls": 0}
    async with SessionLocal() as s:
        state = await get_state(s, owner_id)
        if state.stopped:
            raise RuntimeError("Agent is stopped. Resume it first.")
        prof = await get_profile(s, owner_id)
        preferred = csv(prof.preferred_roles)
        queries = [custom_query] if custom_query else queries_of(state)
        channels = csv(state.tg_channels)
        await s.commit()

    raws: list[S.RawLead] = []
    if "google" in sources:
        found, errs = await S.google_search(queries)
        summary["errors"] += errs
        summary["by_source"]["google"] = len(found)
        raws += found
        # read the actual pages (contacts, real text) - limited to keep runs fast
        todo = [r for r in found if not r.checked][:25]
        await asyncio.gather(*(S.enrich_page(r) for r in todo))
    if "x" in sources:
        try:
            xq = custom_query and f"({custom_query}) -is:retweet lang:en"
            found = await S.x_search(xq or "")
            summary["by_source"]["x"] = len(found)
            raws += found
        except S.ProviderError as e:
            summary["errors"].append(str(e))
    if "telegram" in sources and not is_admin(owner_id):
        summary["errors"].append("Telegram channel search is admin-only.")
    elif "telegram" in sources:
        if not channels:
            summary["errors"].append("No public Telegram channels listed in Agent settings.")
        elif not S.backoff.allowed("telegram"):
            summary["errors"].append(f"telegram: skipped (backing off {S.backoff.wait_left('telegram')}s after errors)")
        else:
            try:
                found = await S.telegram_channels(owner_id, channels)
                summary["by_source"]["telegram"] = len(found)
                raws += found
                S.backoff.ok("telegram")
            except tg.SendBlocked as e:
                S.backoff.fail("telegram", e.message)
                summary["errors"].append(e.message)
            except Exception as e:
                S.backoff.fail("telegram", type(e).__name__)
                summary["errors"].append(f"Telegram: {type(e).__name__}")

    # Free-tier friendly: rank by the deterministic rules first, only the top N go to the AI.
    def rule_score(r: S.RawLead) -> int:
        try:
            return Q.rule_qualify(r.text, r.title, r.project, r.posted_at, r.author_x, r.author_tg, preferred).relevance
        except Exception:
            return 0
    raws.sort(key=rule_score, reverse=True)
    budget = {"left": max(0, min(get_settings().llm_max_calls_per_run, llm.budget_left()))}
    start_budget = budget["left"]

    async with SessionLocal() as s:
        for raw in raws:
            if (await get_state(s, owner_id)).stopped:
                summary["errors"].append("Stopped by you.")
                break
            try:
                lead, outcome = await store_lead(s, owner_id, raw, preferred, llm_budget=budget)
                summary[outcome] += 1
                await s.commit()
                if lead is not None:
                    summary["new_ids"].append(lead.id)
            except Exception as e:
                await s.rollback()
                log.warning("store lead failed: %s", type(e).__name__)
        summary["ai_calls"] = start_budget - budget["left"]
        st = await get_state(s, owner_id)
        st.last_search_at = utcnow()
        label = "Auto search" if trigger != "manual" else "Search"
        await add_log(owner_id, "auto" if trigger != "manual" else "search",
                      f"{label} ({', '.join(sources)}): {summary['new']} new, {summary['duplicate']} duplicates, "
                      f"{summary['irrelevant']} not relevant" + (f"; issues: {'; '.join(summary['errors'])}" if summary["errors"] else ""),
                      "warn" if summary["errors"] else "info", s)
        await s.commit()
    return summary


def start_discovery(owner_id: int, sources: list[str], custom_query: str = "") -> bool:
    t = _running.get(owner_id)
    if t and not t.done():
        return False

    async def runner():
        try:
            await run_discovery(owner_id, sources, custom_query)
        except Exception as e:
            await add_log(owner_id, "error", f"Search failed: {e}", "error")

    _running[owner_id] = asyncio.create_task(runner())
    return True


def discovery_running(owner_id: int) -> bool:
    t = _running.get(owner_id)
    return bool(t and not t.done())


# ------------------------------------------------------------------ drafts
def norm_recipient(channel: str, r: str) -> str:
    return r.strip().lstrip("@").lower()


async def already_contacted(s, owner_id: int, channel: str, recipient: str) -> bool:
    return bool(await s.scalar(select(ContactedRecipient.id).where(
        ContactedRecipient.owner_id == owner_id, ContactedRecipient.channel == channel,
        ContactedRecipient.recipient == norm_recipient(channel, recipient))))


async def create_draft(s, owner_id: int, lead: Lead, contact_id: int | None = None) -> Draft:
    contacts = (await s.scalars(select(Contact).where(Contact.lead_id == lead.id))).all()
    contact = None
    if contact_id:
        contact = next((c for c in contacts if c.id == contact_id), None)
    if contact is None:
        order = {"telegram": 0, "email": 1, "x": 2}
        cands = sorted([c for c in contacts if c.platform in order], key=lambda c: order[c.platform])
        for c in cands:
            if not await already_contacted(s, owner_id, c.platform, c.handle):
                contact = c
                break
    prof = await get_profile(s, owner_id)
    text, by = await drafting.generate(prof, lead)
    channel = contact.platform if contact else "manual"
    d = Draft(owner_id=owner_id, lead_id=lead.id, contact_id=contact.id if contact else None, channel=channel,
              recipient=contact.handle if contact else "", text=text, generated_by=by)
    if contact and await already_contacted(s, owner_id, contact.platform, contact.handle):
        d.status, d.error = "skipped", "Already contacted this recipient before."
    s.add(d)
    await s.flush()
    await add_log(owner_id, "draft", f"Draft #{d.id} for {lead.project or lead.title} → "
                                     f"{channel}:{d.recipient or '(no public contact)'} ({by})", s=s)
    return d


# ------------------------------------------------------------------ sending
async def sent_today(s, owner_id: int) -> int:
    since = utcnow() - timedelta(hours=24)
    return await s.scalar(select(func.count(Draft.id)).where(
        Draft.owner_id == owner_id, Draft.channel == "telegram", Draft.status == "sent", Draft.sent_at >= since)) or 0


async def try_send(owner_id: int, draft_id: int) -> dict:
    """Sends one approved Telegram draft if every guard passes."""
    async with _send_lock:
        async with SessionLocal() as s:
            d = await s.get(Draft, draft_id)
            if not d or d.owner_id != owner_id:
                return {"ok": False, "code": "not_found", "message": "Draft not found."}
            st = await get_state(s, owner_id)
            if d.status != "approved":
                return {"ok": False, "code": "not_approved", "message": "Approve the draft first."}
            if d.channel != "telegram":
                return {"ok": False, "code": "manual", "message": "Only Telegram drafts can be sent automatically."}
            if not is_admin(owner_id):
                return {"ok": False, "code": "admin_only", "message": "Sending from a Telegram account is admin-only."}
            if st.stopped:
                return {"ok": False, "code": "stopped", "message": "Agent is stopped. Resume it to send."}
            if await already_contacted(s, owner_id, "telegram", d.recipient):
                d.status, d.error = "skipped", "Already contacted this recipient before."
                await s.commit()
                return {"ok": False, "code": "duplicate", "message": d.error}
            if await sent_today(s, owner_id) >= st.daily_limit:
                return {"ok": False, "code": "daily_limit", "message": f"Daily limit of {st.daily_limit} reached. "
                                                                        "It stays approved and sends tomorrow."}
            if st.last_sent_at and (utcnow() - st.last_sent_at).total_seconds() < st.min_interval_sec:
                wait = int(st.min_interval_sec - (utcnow() - st.last_sent_at).total_seconds())
                return {"ok": False, "code": "interval", "message": f"Queued: sends in about {wait}s (spacing rule).",
                        "wait": wait}
            d.status = "sending"
            st.last_sent_at = utcnow()
            await s.commit()
            recipient, text = d.recipient, d.text
        try:
            await tg.send_dm(owner_id, recipient, text)
        except tg.SendBlocked as e:
            async with SessionLocal() as s:
                d = await s.get(Draft, draft_id)
                d.status, d.error = "failed", e.message
                if e.stop_agent:
                    (await get_state(s, owner_id)).stopped = True
                await add_log(owner_id, "send", f"Draft #{draft_id} to @{recipient} failed: {e.message}", "error", s)
                await s.commit()
            return {"ok": False, "code": e.code, "message": e.message}
        except Exception as e:
            async with SessionLocal() as s:
                d = await s.get(Draft, draft_id)
                d.status, d.error = "failed", f"Unexpected error: {type(e).__name__}"
                await add_log(owner_id, "send", f"Draft #{draft_id} failed: {type(e).__name__}", "error", s)
                await s.commit()
            return {"ok": False, "code": "error", "message": "Sending failed unexpectedly."}
        async with SessionLocal() as s:
            d = await s.get(Draft, draft_id)
            d.status, d.sent_at, d.error = "sent", utcnow(), ""
            s.add(ContactedRecipient(owner_id=owner_id, channel="telegram",
                                     recipient=norm_recipient("telegram", recipient), draft_id=draft_id))
            lead = await s.get(Lead, d.lead_id)
            if lead and lead.status in ("new", "saved"):
                lead.status = "applied"
            await add_log(owner_id, "send", f"Sent draft #{draft_id} to @{recipient} from your Telegram account", s=s)
            await s.commit()
        return {"ok": True, "code": "sent", "message": f"Sent to @{recipient} ✅"}


async def mark_manual_sent(owner_id: int, draft_id: int) -> dict:
    async with SessionLocal() as s:
        d = await s.get(Draft, draft_id)
        if not d or d.owner_id != owner_id:
            return {"ok": False, "message": "Draft not found."}
        d.status, d.sent_at = "sent", utcnow()
        if d.recipient and not await already_contacted(s, owner_id, d.channel, d.recipient):
            s.add(ContactedRecipient(owner_id=owner_id, channel=d.channel,
                                     recipient=norm_recipient(d.channel, d.recipient), draft_id=d.id))
        lead = await s.get(Lead, d.lead_id)
        if lead and lead.status in ("new", "saved"):
            lead.status = "applied"
        await add_log(owner_id, "send", f"Marked draft #{d.id} as sent manually via {d.channel}", s=s)
        await s.commit()
    return {"ok": True}


async def sender_tick():
    """Background: sends queued approved Telegram drafts (one per tick per owner)."""
    async with SessionLocal() as s:
        rows = (await s.execute(select(Draft.owner_id, func.min(Draft.id)).where(
            Draft.status == "approved", Draft.channel == "telegram").group_by(Draft.owner_id))).all()
    for owner_id, draft_id in rows:
        if not is_admin(owner_id):
            continue
        res = await try_send(owner_id, draft_id)
        if not res["ok"] and res["code"] not in ("interval", "daily_limit", "stopped"):
            log.info("queued send %s: %s", draft_id, res["code"])


async def stop_agent(owner_id: int, stop: bool = True):
    async with SessionLocal() as s:
        st = await get_state(s, owner_id)
        st.stopped = stop
        await add_log(owner_id, "agent", "Agent stopped by you" if stop else "Agent resumed", s=s)
        await s.commit()
    if stop:
        t = _running.get(owner_id)
        if t and not t.done():
            t.cancel()
