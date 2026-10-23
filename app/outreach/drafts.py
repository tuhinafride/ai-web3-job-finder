"""Personalised outreach message drafting. Never invents qualifications.

AI drafts are checked: any number in the draft must appear in the owner's profile or in the
job post; otherwise the AI draft is rejected and the safe template is used instead.
"""
from __future__ import annotations

import re

from . import llm
from .models import ROLE_LABELS

MAX_LEN = 1200


def profile_facts(p) -> str:
    return (f"Name: {p.name}\nLocation: {p.location}\nHeadline: {p.headline}\nExperience: {p.experience}\n"
            f"Skills: {p.skills}\nLinks: {p.links}")


def template_draft(p, lead) -> str:
    """Owner's template, lightly personalised with only facts from the post."""
    role_name = ROLE_LABELS.get(lead.role, "")
    project = (lead.project or "").strip()
    text = (p.template or "").strip()
    if project:
        if text.startswith("Hi,"):
            text = text.replace("Hi,", f"Hi {project} team,", 1)
        text = text.replace("if your project currently", f"if {project} currently")
    if role_name and lead.role != "other":
        text = re.sub(r"opportunities for a [^.?]+? contributor", f"opportunities for a {role_name} role", text)
    return text[:MAX_LEN]


DRAFT_SYSTEM = """You write short, polite job inquiry messages for a Web3 job seeker.
Rules:
- Use ONLY the facts in PROFILE. Never invent jobs, employers, numbers, achievements, links or skills.
- Mention the project by name and refer to one or two real requirements or details from POST.
- Ask whether they have a suitable PAID opportunity for the matching role.
- 70 to 130 words, plain text, no hashtags, no emojis, no subject line, no placeholders.
- If the post asks to apply through a specific form or email, politely mention that you can apply there too.
Return JSON: {"message": "..."}"""


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+", s or ""))


def safe_ai_text(text: str, p, lead) -> bool:
    allowed = _numbers(profile_facts(p)) | _numbers(lead.raw_text) | _numbers(lead.title) | _numbers(lead.project)
    allowed |= _numbers(p.template)
    return bool(text) and _numbers(text) <= allowed and len(text) <= MAX_LEN and "[" not in text


async def generate(p, lead) -> tuple[str, str]:
    """Returns (text, generated_by)."""
    if llm.enabled("draft"):
        post = (f"Project: {lead.project}\nTitle: {lead.title}\nRole: {ROLE_LABELS.get(lead.role, '')}\n"
                f"Responsibilities: {lead.responsibilities}\nRequirements: {lead.requirements}\n"
                f"Pay: {lead.pay_status} {lead.pay_detail}\nApply: {lead.apply_method}\n"
                f"Post text: {lead.raw_text[:2500]}")
        try:
            data = await llm.chat_json(DRAFT_SYSTEM, f"PROFILE:\n{profile_facts(p)}\n\nPOST:\n{post}", max_tokens=500, purpose="draft")
            text = str(data.get("message") or "").strip()
            if safe_ai_text(text, p, lead):
                return text, "ai"
        except llm.LLMUnavailable:
            pass
    return template_draft(p, lead), "template"
