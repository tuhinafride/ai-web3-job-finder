"""Database models for the AI job discovery + outreach agent (owner only)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, utcnow

# Target role keys for outreach (independent from the public job categories)
ROLE_LABELS = {
    "super_moderator": "Super Moderator",
    "moderator": "Discord / Telegram Moderator",
    "community": "Community Manager",
    "support": "Customer Support",
    "social": "Social Media Manager",
    "bd_growth": "Business Development & Growth",
    "community_ops": "Community Operations",
    "ambassador": "Ambassador / Paid Contributor",
    "other": "Other Web3 role",
}

DEFAULT_PROFILE = {
    "name": "Tuhin Afride",
    "location": "Bangladesh (GMT+6)",
    "headline": "Web3 community moderation and management specialist",
    "experience": ("4+ years in Web3 community moderation and management. Managed and moderated "
                   "communities of 100K+ members. Worked with 25+ Web3 projects over about five years."),
    "skills": ("Discord moderation, Telegram community management, user support, community growth, "
               "X engagement, announcements, campaign coordination, airdrop support, business development"),
    "preferred_roles": "moderator,super_moderator,community,support,social,bd_growth",
    "links": "",
    "template": (
        "Hi, I'm Tuhin Afride, a Web3 community management and moderation specialist with 4+ years "
        "of experience supporting Web3 communities.\n\n"
        "I have experience with Discord and Telegram moderation, community support, campaign "
        "coordination, and community growth.\n\n"
        "I wanted to ask if your project currently has any paid opportunities for a Moderator, "
        "Community Manager, or Customer Support contributor.\n\n"
        "I'd be happy to share my experience and discuss how I could contribute to your team.\n\n"
        "Thank you for your time."),
}

DEFAULT_QUERIES = [
    "Web3 moderator hiring",
    "Crypto community manager hiring",
    "Discord moderator paid job crypto",
    "Telegram community manager vacancy crypto",
    "Web3 startup hiring contributors community",
    "Crypto project looking for community support",
]


class OwnerProfile(Base):
    __tablename__ = "owner_profiles"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(120), default="")
    headline: Mapped[str] = mapped_column(String(200), default="")
    experience: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[str] = mapped_column(Text, default="")
    preferred_roles: Mapped[str] = mapped_column(Text, default="")
    links: Mapped[str] = mapped_column(Text, default="")
    template: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentState(Base):
    __tablename__ = "agent_state"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_limit: Mapped[int] = mapped_column(Integer, default=10)
    min_interval_sec: Mapped[int] = mapped_column(Integer, default=180)
    auto_search_hours: Mapped[int] = mapped_column(Integer, default=0)   # 0 = manual only
    queries: Mapped[str] = mapped_column(Text, default="")               # newline separated
    tg_channels: Mapped[str] = mapped_column(Text, default="")           # public channel usernames
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Lead(Base):
    """One discovered opportunity (post, listing or announcement)."""
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    uid: Mapped[str] = mapped_column(String(80), index=True)          # dedupe key (normalised URL)
    fp: Mapped[str] = mapped_column(String(40), default="", index=True)  # near-duplicate text key
    source: Mapped[str] = mapped_column(String(24))                   # google | x | telegram | board
    source_url: Mapped[str] = mapped_column(Text)
    source_checked: Mapped[bool] = mapped_column(Boolean, default=False)  # we fetched the page OK
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    project: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    role: Mapped[str] = mapped_column(String(32), default="other", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    responsibilities: Mapped[str] = mapped_column(Text, default="")   # newline bullets
    requirements: Mapped[str] = mapped_column(Text, default="")
    pay_status: Mapped[str] = mapped_column(String(16), default="unknown")   # paid|unpaid|unknown
    pay_detail: Mapped[str] = mapped_column(String(200), default="")
    apply_method: Mapped[str] = mapped_column(String(300), default="")
    deadline: Mapped[str] = mapped_column(String(80), default="")
    remote: Mapped[str] = mapped_column(String(16), default="unknown")       # yes|no|unknown
    relevance: Mapped[int] = mapped_column(Integer, default=0, index=True)
    scam_risk: Mapped[str] = mapped_column(String(8), default="low")         # low|medium|high
    scam_flags: Mapped[str] = mapped_column(Text, default="")
    freshness: Mapped[str] = mapped_column(String(12), default="unknown")    # current|old|unknown
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    # new | saved | applied | interview | offer | rejected | skipped
    notes: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("owner_id", "uid"),)


class Contact(Base):
    __tablename__ = "lead_contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16))     # telegram | x | email | discord | website
    handle: Mapped[str] = mapped_column(String(200))      # username without @, email, or url
    name: Mapped[str] = mapped_column(String(200), default="")
    role_hint: Mapped[str] = mapped_column(String(120), default="")   # e.g. "post author", "listed in post"
    found_at_url: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("lead_id", "platform", "handle"),)


class Draft(Base):
    __tablename__ = "outreach_drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    lead_id: Mapped[int] = mapped_column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str] = mapped_column(String(16))      # telegram | x | email
    recipient: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(16), default="template")  # ai | template | edited
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | approved | sending | sent | failed | skipped | rejected
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ContactedRecipient(Base):
    """Every recipient we ever messaged (or marked as messaged) - prevents repeats."""
    __tablename__ = "contacted_recipients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(200))   # normalised lowercase
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner_id", "channel", "recipient"),)


class OutreachLog(Base):
    __tablename__ = "outreach_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(16))      # search | qualify | draft | send | agent | error
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
