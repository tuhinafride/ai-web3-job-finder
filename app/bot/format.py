"""Telegram message formatting (HTML parse mode, everything escaped)."""
from __future__ import annotations

from html import escape

from ..classify import CATEGORY_LABELS, trim
from ..db import Job


def _e(v) -> str:
    return escape(str(v or ""), quote=False)


def source_badge(job: Job) -> str:
    if job.source_type == "company_ats":
        return "🏢 Direct from company careers board"
    return "🔎 Third-party listing: verify on the company site before applying"


def job_card(job: Job, score: int | None = None, full: bool = False) -> str:
    lines = [f"<b>{_e(job.title)}</b>", f"🏷 {_e(job.company)}"]
    loc = job.location or ("Remote" if job.remote else "")
    if loc:
        lines.append(f"📍 {_e(trim(loc, 80))}{' · 🌍 Remote' if job.remote and 'remote' not in loc.lower() else ''}")
    lines.append(f"💼 {_e(CATEGORY_LABELS.get(job.category, 'Other'))}")
    lines.append(f"💰 {_e(job.salary) if job.salary else 'Salary not listed'}")
    if score is not None:
        lines.append(f"🎯 Match: {score}%")
    reqs = [r for r in (job.requirements or '').split('\n') if r]
    if reqs:
        lines.append("\n<b>Requirements</b>")
        for r in reqs[: (6 if full else 3)]:
            lines.append(f"• {_e(trim(r, 160))}")
    if full and job.description and not reqs:
        lines.append("\n" + _e(trim(job.description, 700)))
    lines.append(f"\n{source_badge(job)}")
    lines.append(f"Source: {_e(job.source_name)}")
    lines.append(f'<a href="{escape(job.url, quote=True)}">View listing</a> · '
                 f'<a href="{escape(job.apply_url, quote=True)}">Apply</a>')
    return "\n".join(lines)


def channel_post(job: Job) -> str:
    """Formatted announcement for the admin channel."""
    lines = [
        "🚀 <b>New Web3 Opportunity</b>", "",
        f"<b>{_e(job.title)}</b>",
        f"🏢 {_e(job.company)}",
        f"📍 {_e(trim(job.location or ('Remote' if job.remote else 'Not specified'), 80))}",
        f"💼 {_e(CATEGORY_LABELS.get(job.category, 'Other'))}",
    ]
    if job.salary:
        lines.append(f"💰 {_e(job.salary)}")
    reqs = [r for r in (job.requirements or '').split('\n') if r][:4]
    if reqs:
        lines += ["", "<b>What they look for</b>"] + [f"• {_e(trim(r, 150))}" for r in reqs]
    lines += ["", source_badge(job), f'👉 <a href="{escape(job.apply_url, quote=True)}">Apply here</a>',
              "", "#web3jobs #" + job.category]
    return "\n".join(lines)


def custom_post(title: str, company: str, body: str, link: str) -> str:
    return "\n".join([
        "📢 <b>Web3 Job Announcement</b>", "",
        f"<b>{_e(title)}</b>", f"🏢 {_e(company)}", "", _e(body), "",
        f'👉 <a href="{escape(link, quote=True)}">Details / Apply</a>', "",
        "ℹ️ Posted by channel admin. Always verify on the company's official site.",
    ])
