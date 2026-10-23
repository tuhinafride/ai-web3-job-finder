"""REST API for the AI job agent (mounted under /api/agent).

Every active user can use it; all data is scoped by the verified Telegram user id (owner_id).
Sending from the personal Telegram account, Telegram-channel discovery and auto-search stay
admin-only. Paid provider calls (search runs, drafts) are limited per user per day.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..config import get_settings
from ..db import Job, SessionLocal, utcnow
from .. import accounts as acc
from ..auth import current_user
from . import agent as A
from . import search as S
from .models import (ROLE_LABELS, AgentState, ContactedRecipient, Contact, Draft, Lead, OutreachLog,
                     OwnerProfile)

router = APIRouter(prefix="/api/agent")


async def owner(user: dict = Depends(current_user)) -> int:
    """The verified id of the calling user. Every query below filters by it."""
    return user["id"]


ADMIN_ONLY_SOURCES = {"telegram"}   # reads channels through the owner's personal account


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def lead_dict(l: Lead, contacts: list[Contact] | None = None, drafts: list[Draft] | None = None) -> dict:
    return {
        "id": l.id, "source": l.source, "source_url": l.source_url, "source_checked": l.source_checked,
        "discovered_at": iso(l.discovered_at), "posted_at": iso(l.posted_at), "project": l.project,
        "title": l.title, "role": l.role, "role_label": ROLE_LABELS.get(l.role, "Other"), "summary": l.summary,
        "responsibilities": [x for x in l.responsibilities.split("\n") if x],
        "requirements": [x for x in l.requirements.split("\n") if x],
        "pay_status": l.pay_status, "pay_detail": l.pay_detail, "apply_method": l.apply_method,
        "deadline": l.deadline, "remote": l.remote, "relevance": l.relevance, "scam_risk": l.scam_risk,
        "scam_flags": [x for x in l.scam_flags.split("\n") if x], "freshness": l.freshness, "ai_used": l.ai_used,
        "status": l.status, "notes": l.notes,
        "contacts": [{"id": c.id, "platform": c.platform, "handle": c.handle, "role_hint": c.role_hint,
                      "found_at_url": c.found_at_url} for c in (contacts or [])],
        "drafts": [draft_dict(d) for d in (drafts or [])],
    }


def draft_dict(d: Draft, lead: Lead | None = None) -> dict:
    out = {"id": d.id, "lead_id": d.lead_id, "channel": d.channel, "recipient": d.recipient, "text": d.text,
           "generated_by": d.generated_by, "status": d.status, "error": d.error,
           "created_at": iso(d.created_at), "sent_at": iso(d.sent_at)}
    if lead is not None:
        out.update(project=lead.project, title=lead.title, source=lead.source, source_url=lead.source_url,
                   scam_risk=lead.scam_risk, relevance=lead.relevance)
    return out


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
async def dashboard(owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        st = await A.get_state(s, owner_id)
        await A.get_profile(s, owner_id)
        await s.commit()
        count = lambda *w: s.scalar(select(func.count()).select_from(Lead).where(Lead.owner_id == owner_id, *w))
        dcount = lambda *w: s.scalar(select(func.count()).select_from(Draft).where(Draft.owner_id == owner_id, *w))
        week = utcnow() - timedelta(days=7)
        data = {
            "leads_total": await count(), "leads_week": await count(Lead.discovered_at >= week),
            "paid": await count(Lead.pay_status == "paid"), "flagged": await count(Lead.scam_risk != "low"),
            "saved": await count(Lead.status == "saved"), "applied": await count(Lead.status.in_(["applied", "interview", "offer"])),
            "pending": await dcount(Draft.status == "pending"), "approved": await dcount(Draft.status == "approved"),
            "sent": await dcount(Draft.status == "sent"), "failed": await dcount(Draft.status == "failed"),
            "sent_today": await A.sent_today(s, owner_id), "board_jobs": await s.scalar(select(func.count(Job.id))),
        }
        logs = (await s.scalars(select(OutreachLog).where(OutreachLog.owner_id == owner_id)
                                .order_by(OutreachLog.id.desc()).limit(5))).all()
    is_admin = acc.is_admin_id(owner_id)
    if is_admin:
        from .. import userclient as uc
        acct = await uc.status(owner_id)
    else:
        acct = {"connected": False}
    async with SessionLocal() as s:
        limits = await acc.limits_for(s, owner_id)
    return {**data, "is_admin": is_admin, "limits": limits, "agent": {"stopped": st.stopped, "daily_limit": st.daily_limit,
                              "min_interval_sec": st.min_interval_sec, "running": A.discovery_running(owner_id),
                              "last_search_at": iso(st.last_search_at)},
            "providers": S.configured() if is_admin else {}, "telegram_connected": acct["connected"],
            "recent_logs": [{"ts": iso(l.ts), "kind": l.kind, "level": l.level, "message": l.message} for l in logs]}


# ------------------------------------------------------------------ discovery
class SearchIn(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["google", "x", "telegram"], max_length=4)
    query: str = Field("", max_length=200)


@router.post("/search")
async def search(body: SearchIn, owner_id: int = Depends(owner)):
    sources = [x for x in body.sources if x in ("google", "x", "telegram")]
    if not acc.is_admin_id(owner_id):
        if sources and all(x in ADMIN_ONLY_SOURCES for x in sources):
            raise HTTPException(403, "Telegram channel search uses the owner's account and is admin-only.")
        sources = [x for x in sources if x not in ADMIN_ONLY_SOURCES]
    if not sources:
        raise HTTPException(400, "Pick at least one source.")
    async with SessionLocal() as s:
        if (await A.get_state(s, owner_id)).stopped:
            raise HTTPException(409, "Agent is stopped. Resume it first.")
        if A.discovery_running(owner_id):
            return {"started": False, "running": True, "message": "A search is already running."}
        await acc.consume(s, owner_id, "search")      # raises LimitReached -> 429
        await s.commit()
    started = A.start_discovery(owner_id, sources, body.query.strip())
    return {"started": started, "running": True,
            "message": "Searching… results appear in a minute." if started else "A search is already running."}


@router.get("/leads")
async def leads(owner_id: int = Depends(owner), source: str = "", role: str = "", pay: str = "",
                status: str = "", min_relevance: int = Query(0, ge=0, le=100), freshness: str = "",
                hide_flagged: bool = False, q: str = Query("", max_length=100), sort: str = "relevance",
                offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
    w = [Lead.owner_id == owner_id]
    if source:
        w.append(Lead.source == source)
    if role:
        w.append(Lead.role == role)
    if pay:
        w.append(Lead.pay_status == pay)
    if status:
        w.append(Lead.status.in_(status.split(",")))
    else:
        w.append(Lead.status != "skipped")
    if freshness:
        w.append(Lead.freshness == freshness)
    if min_relevance:
        w.append(Lead.relevance >= min_relevance)
    if hide_flagged:
        w.append(Lead.scam_risk == "low")
    if q:
        like = f"%{q}%"
        w.append(or_(Lead.project.ilike(like), Lead.title.ilike(like), Lead.summary.ilike(like)))
    order = {"relevance": Lead.relevance.desc(), "date": Lead.discovered_at.desc(),
             "posted": Lead.posted_at.desc().nullslast()}.get(sort, Lead.relevance.desc())
    async with SessionLocal() as s:
        total = await s.scalar(select(func.count()).select_from(Lead).where(*w))
        rows = (await s.scalars(select(Lead).where(*w).order_by(order, Lead.id.desc()).offset(offset).limit(limit))).all()
        ids = [l.id for l in rows]
        contacts = (await s.scalars(select(Contact).where(Contact.lead_id.in_(ids)))).all() if ids else []
    by = {}
    for c in contacts:
        by.setdefault(c.lead_id, []).append(c)
    return {"total": total, "leads": [lead_dict(l, by.get(l.id, [])) for l in rows], "roles": ROLE_LABELS}


@router.get("/leads/{lead_id}")
async def lead_detail(lead_id: int, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        l = await s.get(Lead, lead_id)
        if not l or l.owner_id != owner_id:
            raise HTTPException(404, "Not found")
        contacts = (await s.scalars(select(Contact).where(Contact.lead_id == l.id))).all()
        drafts = (await s.scalars(select(Draft).where(Draft.lead_id == l.id).order_by(Draft.id.desc()))).all()
        out = lead_dict(l, contacts, drafts)
        for c in out["contacts"]:
            c["contacted"] = await A.already_contacted(s, owner_id, c["platform"], c["handle"])
        return out


class LeadUpdate(BaseModel):
    status: str | None = None
    notes: str | None = Field(None, max_length=2000)


@router.patch("/leads/{lead_id}")
async def lead_update(lead_id: int, body: LeadUpdate, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        l = await s.get(Lead, lead_id)
        if not l or l.owner_id != owner_id:
            raise HTTPException(404, "Not found")
        if body.status is not None:
            if body.status not in ("new", "saved", "applied", "interview", "offer", "rejected", "skipped"):
                raise HTTPException(400, "Bad status")
            l.status = body.status
        if body.notes is not None:
            l.notes = body.notes
        await s.commit()
        return {"ok": True, "status": l.status}


@router.post("/jobs/{job_id}/import")
async def import_job(job_id: int, owner_id: int = Depends(owner)):
    """Turns a company-board job into a lead so it can be tracked and drafted."""
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        prof = await A.get_profile(s, owner_id)
        # AI scoring on import is a paid call: admins only (normal users get rule-based scoring)
        lead = await A.import_board_job(s, owner_id, job, A.csv(prof.preferred_roles),
                                        use_llm=acc.is_admin_id(owner_id))
        await s.commit()
        return {"lead_id": lead.id}


class DraftCreate(BaseModel):
    contact_id: int | None = None


@router.post("/leads/{lead_id}/draft")
async def make_draft(lead_id: int, body: DraftCreate, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        l = await s.get(Lead, lead_id)
        if not l or l.owner_id != owner_id:
            raise HTTPException(404, "Not found")
        if l.scam_risk == "high":
            raise HTTPException(409, "This opportunity has high scam risk. Drafting is disabled for it.")
        await acc.consume(s, owner_id, "draft")       # raises LimitReached -> 429
        d = await A.create_draft(s, owner_id, l, body.contact_id)
        await s.commit()
        return draft_dict(d, l)


# ------------------------------------------------------------------ drafts
@router.get("/drafts")
async def list_drafts(owner_id: int = Depends(owner), status: str = "pending",
                      offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=50)):
    statuses = status.split(",")
    async with SessionLocal() as s:
        rows = (await s.execute(select(Draft, Lead).join(Lead, Lead.id == Draft.lead_id)
                                .where(Draft.owner_id == owner_id, Draft.status.in_(statuses))
                                .order_by(Draft.id.desc()).offset(offset).limit(limit))).all()
    return {"drafts": [draft_dict(d, l) for d, l in rows]}


class DraftAction(BaseModel):
    action: str                 # edit | approve | skip | reject | mark_sent | retry
    text: str | None = Field(None, max_length=1500)


@router.post("/drafts/{draft_id}")
async def draft_action(draft_id: int, body: DraftAction, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        d = await s.get(Draft, draft_id)
        if not d or d.owner_id != owner_id:
            raise HTTPException(404, "Not found")
        if d.status == "sent" and body.action != "mark_sent":
            raise HTTPException(409, "Already sent.")
        if body.action == "edit":
            if not (body.text or "").strip():
                raise HTTPException(400, "Message can't be empty.")
            d.text, d.generated_by = body.text.strip(), "edited"
            if d.status in ("failed", "skipped", "rejected"):
                d.status = "pending"
        elif body.action in ("skip", "reject"):
            d.status = "skipped" if body.action == "skip" else "rejected"
        elif body.action in ("approve", "retry"):
            if d.channel == "telegram" and not acc.is_admin_id(owner_id):
                raise HTTPException(403, "Sending from a connected Telegram account is admin-only. "
                                         "Copy the message, open the chat, then tap Mark as sent.")
            if body.text and body.text.strip() and body.text.strip() != d.text.strip():
                d.text, d.generated_by = body.text.strip(), "edited"
            if d.channel == "telegram" and await A.already_contacted(s, owner_id, "telegram", d.recipient):
                d.status, d.error = "skipped", "Already contacted this recipient before."
                await s.commit()
                return {"ok": False, "status": d.status, "message": d.error}
            d.status, d.error = "approved", ""
        elif body.action != "mark_sent":
            raise HTTPException(400, "Unknown action")
        await A.add_log(owner_id, "draft", f"Draft #{d.id}: {body.action}", s=s)
        await s.commit()
        channel = d.channel
    if body.action == "mark_sent":
        res = await A.mark_manual_sent(owner_id, draft_id)
        return {"ok": res["ok"], "status": "sent"}
    if body.action in ("approve", "retry") and channel == "telegram":
        res = await A.try_send(owner_id, draft_id)
        async with SessionLocal() as s:
            d = await s.get(Draft, draft_id)
        return {"ok": res["ok"], "status": d.status, "message": res["message"]}
    return {"ok": True, "status": body.action}


# ------------------------------------------------------------------ contacts / logs
@router.get("/contacts")
async def contacts(owner_id: int = Depends(owner), platform: str = "", limit: int = Query(50, le=100)):
    async with SessionLocal() as s:
        w = [Lead.owner_id == owner_id, Lead.status != "skipped"]
        if platform:
            w.append(Contact.platform == platform)
        rows = (await s.execute(select(Contact, Lead).join(Lead, Lead.id == Contact.lead_id).where(*w)
                                .order_by(Lead.relevance.desc()).limit(limit))).all()
        contacted = {(c.channel, c.recipient) for c in (await s.scalars(
            select(ContactedRecipient).where(ContactedRecipient.owner_id == owner_id))).all()}
    return {"contacts": [{"id": c.id, "platform": c.platform, "handle": c.handle, "role_hint": c.role_hint,
                          "found_at_url": c.found_at_url, "lead_id": l.id, "project": l.project, "title": l.title,
                          "scam_risk": l.scam_risk,
                          "contacted": (c.platform, c.handle.lower().lstrip("@")) in contacted} for c, l in rows]}


@router.get("/logs")
async def logs(owner_id: int = Depends(owner), limit: int = Query(80, le=200)):
    async with SessionLocal() as s:
        rows = (await s.scalars(select(OutreachLog).where(OutreachLog.owner_id == owner_id)
                                .order_by(OutreachLog.id.desc()).limit(limit))).all()
    return {"logs": [{"ts": iso(l.ts), "kind": l.kind, "level": l.level, "message": l.message} for l in rows]}


# ------------------------------------------------------------------ profile & settings
class ProfileIn(BaseModel):
    name: str = Field(max_length=120)
    location: str = Field("", max_length=120)
    headline: str = Field("", max_length=200)
    experience: str = Field("", max_length=1500)
    skills: str = Field("", max_length=1000)
    preferred_roles: list[str] = Field(default_factory=list, max_length=9)
    links: str = Field("", max_length=500)
    template: str = Field("", max_length=1500)


@router.get("/profile")
async def get_profile(owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        p = await A.get_profile(s, owner_id)
        await s.commit()
        return {"name": p.name, "location": p.location, "headline": p.headline, "experience": p.experience,
                "skills": p.skills, "preferred_roles": A.csv(p.preferred_roles), "links": p.links,
                "template": p.template, "roles": ROLE_LABELS}


@router.put("/profile")
async def put_profile(body: ProfileIn, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        p = await A.get_profile(s, owner_id)
        for k in ("name", "location", "headline", "experience", "skills", "links", "template"):
            setattr(p, k, getattr(body, k).strip())
        p.preferred_roles = ",".join(r for r in body.preferred_roles if r in ROLE_LABELS)
        await s.commit()
    return {"ok": True}


class SettingsIn(BaseModel):
    daily_limit: int = Field(10, ge=1, le=30)
    min_interval_sec: int = Field(180, ge=60, le=3600)
    auto_search_hours: int = Field(0, ge=0, le=48)
    queries: str = Field("", max_length=2000)
    tg_channels: str = Field("", max_length=1000)


@router.get("/settings")
async def get_settings_(owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        st = await A.get_state(s, owner_id)
        await s.commit()
        return {"stopped": st.stopped, "daily_limit": st.daily_limit, "min_interval_sec": st.min_interval_sec,
                "auto_search_hours": st.auto_search_hours, "queries": st.queries, "tg_channels": st.tg_channels,
                "providers": S.configured() if acc.is_admin_id(owner_id) else {},
                "is_admin": acc.is_admin_id(owner_id)}


@router.put("/settings")
async def put_settings(body: SettingsIn, owner_id: int = Depends(owner)):
    async with SessionLocal() as s:
        st = await A.get_state(s, owner_id)
        st.daily_limit, st.min_interval_sec = body.daily_limit, body.min_interval_sec
        # auto-search spends paid provider calls in the background: admin-only
        st.auto_search_hours = body.auto_search_hours if acc.is_admin_id(owner_id) else 0
        st.queries = "\n".join([q.strip() for q in body.queries.split("\n") if q.strip()][:12])
        st.tg_channels = "\n".join([c.strip().lstrip("@") for c in body.tg_channels.replace(",", "\n").split("\n")
                                    if c.strip()][:15])
        await A.add_log(owner_id, "agent", "Agent settings updated", s=s)
        await s.commit()
    return {"ok": True}


class StopIn(BaseModel):
    stop: bool = True


@router.post("/stop")
async def stop(body: StopIn, owner_id: int = Depends(owner)):
    await A.stop_agent(owner_id, body.stop)
    return {"stopped": body.stop}
