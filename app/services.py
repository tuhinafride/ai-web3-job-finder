"""Business logic shared by the bot and the Mini App API."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .classify import CATEGORY_LABELS
from .db import Job, SavedJob, SentAlert, SessionLocal, User, utcnow
from .sources.providers import fetch_all

log = logging.getLogger(__name__)
PAGE_SIZE = 5


# ---------------- users ----------------
async def get_or_create_user(s: AsyncSession, tg_id: int, username: str | None = None,
                             first_name: str | None = None) -> User:
    user = await s.get(User, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=username, first_name=first_name)
        s.add(user)
        await s.flush()
    else:
        if username is not None:
            user.username = username
        if first_name is not None:
            user.first_name = first_name
    return user


def split_csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def user_profile_dict(u: User) -> dict:
    return {
        "tg_id": u.tg_id, "username": u.username, "first_name": u.first_name,
        "roles": split_csv(u.roles), "skills": split_csv(u.skills),
        "remote_only": u.remote_only, "alerts_enabled": u.alerts_enabled,
    }


# ---------------- jobs ----------------
def job_dict(j: Job, saved: bool = False, score: int | None = None) -> dict:
    return {
        "id": j.id, "title": j.title, "company": j.company, "location": j.location,
        "remote": j.remote, "salary": j.salary, "category": j.category,
        "category_label": CATEGORY_LABELS.get(j.category, "Other"),
        "tags": split_csv(j.tags)[:8],
        "requirements": [r for r in (j.requirements or "").split("\n") if r],
        "description": j.description, "url": j.url, "apply_url": j.apply_url,
        "source_name": j.source_name, "source_type": j.source_type,
        "source_note": source_note(j),
        "posted_at": j.posted_at.isoformat() + "Z" if j.posted_at else None,
        "saved": saved, "match_score": score,
    }


def source_note(j: Job) -> str:
    if j.source_type == "company_ats":
        return "Direct from the company's official careers board."
    return "Third-party listing. Verify on the company's own website before applying, and never pay to apply."


async def search_jobs(s: AsyncSession, q: str = "", category: str = "", remote: bool | None = None,
                      has_salary: bool = False, verified_only: bool = False,
                      offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[Job], int]:
    stmt = select(Job)
    if q:
        for word in q.lower().split()[:6]:
            like = f"%{word}%"
            stmt = stmt.where(or_(func.lower(Job.title).like(like), func.lower(Job.company).like(like),
                                  func.lower(Job.tags).like(like), func.lower(Job.description).like(like)))
    if category:
        stmt = stmt.where(Job.category == category)
    if remote:
        stmt = stmt.where(Job.remote.is_(True))
    if has_salary:
        stmt = stmt.where(Job.salary != "")
    if verified_only:
        stmt = stmt.where(Job.source_type == "company_ats")
    total = (await s.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(Job.posted_at.desc().nulls_last(), Job.id.desc()).offset(offset).limit(limit)
    return list((await s.execute(stmt)).scalars()), total


def match_score(job: Job, roles: list[str], skills: list[str], remote_only: bool) -> int:
    """0-100. Role category match dominates; skills add on; remote preference filters."""
    if remote_only and not job.remote:
        return 0
    score = 0
    if roles and job.category in roles:
        score += 60
    blob = f"{job.title} {job.tags} {job.description[:3000]}".lower()
    if skills:
        hits = sum(1 for sk in skills if sk.lower() in blob)
        score += min(40, int(40 * hits / max(1, min(len(skills), 4))))
    if not roles and not skills:
        score = 10
    return min(score, 100)


async def matched_jobs(s: AsyncSession, user: User, limit: int = 20, since=None) -> list[tuple[Job, int]]:
    roles, skills = split_csv(user.roles), split_csv(user.skills)
    stmt = select(Job)
    if roles:
        stmt = stmt.where(Job.category.in_(roles))
    if user.remote_only:
        stmt = stmt.where(Job.remote.is_(True))
    if since is not None:
        stmt = stmt.where(Job.first_seen >= since)
    stmt = stmt.order_by(Job.posted_at.desc().nulls_last()).limit(300)
    jobs = list((await s.execute(stmt)).scalars())
    scored = [(j, match_score(j, roles, skills, user.remote_only)) for j in jobs]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: (x[1], x[0].posted_at or x[0].first_seen), reverse=True)
    return scored[:limit]


async def saved_ids(s: AsyncSession, tg_id: int) -> set[int]:
    rows = await s.execute(select(SavedJob.job_id).where(SavedJob.tg_id == tg_id))
    return set(rows.scalars())


async def toggle_save(s: AsyncSession, tg_id: int, job_id: int, want: bool | None = None) -> bool:
    """Returns the new saved state."""
    existing = (await s.execute(select(SavedJob).where(SavedJob.tg_id == tg_id, SavedJob.job_id == job_id))).scalar_one_or_none()
    if existing and want is not True:
        await s.delete(existing)
        return False
    if not existing and want is not False:
        if await s.get(Job, job_id) is None:
            raise ValueError("job not found")
        await get_or_create_user(s, tg_id)
        s.add(SavedJob(tg_id=tg_id, job_id=job_id))
        return True
    return bool(existing)


async def list_saved(s: AsyncSession, tg_id: int) -> list[Job]:
    stmt = (select(Job).join(SavedJob, SavedJob.job_id == Job.id)
            .where(SavedJob.tg_id == tg_id).order_by(SavedJob.created_at.desc()))
    return list((await s.execute(stmt)).scalars())


async def stats(s: AsyncSession) -> dict:
    total = (await s.execute(select(func.count(Job.id)))).scalar_one()
    rows = await s.execute(select(Job.category, func.count(Job.id)).group_by(Job.category))
    return {"total": total, "by_category": {k: v for k, v in rows.all()}}


# ---------------- refresh ----------------
async def refresh_jobs() -> tuple[list[int], dict[str, str]]:
    """Fetch all sources, upsert, prune stale. Returns (ids of NEW jobs, source status)."""
    raw, status = await fetch_all()
    now = utcnow()
    new_ids: list[int] = []
    async with SessionLocal() as s:
        existing = {uid: jid for uid, jid in (await s.execute(select(Job.uid, Job.id))).all()}
        for rj in raw:
            row = rj.to_row()
            if row["uid"] in existing:
                job = await s.get(Job, existing[row["uid"]])
                for k, v in row.items():
                    setattr(job, k, v)
                job.last_seen = now
            else:
                job = Job(**row, first_seen=now, last_seen=now)
                s.add(job)
                await s.flush()
                new_ids.append(job.id)
        cutoff = now - timedelta(days=get_retention_days())
        # Only prune when fetching actually worked, so an outage never wipes the DB.
        if raw:
            stale = select(Job.id).where(Job.last_seen < cutoff)
            await s.execute(delete(SavedJob).where(SavedJob.job_id.in_(stale)))
            await s.execute(delete(Job).where(Job.last_seen < cutoff))
        await s.commit()
    log.info("refresh: %d fetched, %d new, sources=%s", len(raw), len(new_ids), status)
    return new_ids, status


def get_retention_days() -> int:
    from .config import get_settings
    return get_settings().job_retention_days


async def pending_alerts(s: AsyncSession, new_ids: list[int], per_user: int = 5) -> dict[int, list[Job]]:
    """For each opted-in user, the new jobs that match their profile and were not sent before."""
    if not new_ids:
        return {}
    users = list((await s.execute(select(User).where(User.alerts_enabled.is_(True)))).scalars())
    jobs = list((await s.execute(select(Job).where(Job.id.in_(new_ids)))).scalars())
    out: dict[int, list[Job]] = {}
    for u in users:
        roles, skills = split_csv(u.roles), split_csv(u.skills)
        if not roles and not skills:
            continue  # alerts only fire for a real profile, never spam-everything
        already = set((await s.execute(select(SentAlert.job_id).where(SentAlert.tg_id == u.tg_id))).scalars())
        picks = [j for j in jobs if j.id not in already and match_score(j, roles, skills, u.remote_only) >= 40]
        picks.sort(key=lambda j: match_score(j, roles, skills, u.remote_only), reverse=True)
        if picks:
            out[u.tg_id] = picks[:per_user]
    return out
