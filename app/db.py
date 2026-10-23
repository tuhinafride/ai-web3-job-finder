"""Database models and session helpers (SQLite by default, PostgreSQL in production)."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    roles: Mapped[str] = mapped_column(Text, default="")        # comma-separated category keys
    skills: Mapped[str] = mapped_column(Text, default="")       # comma-separated free text
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(200), unique=True, index=True)  # source:external_id
    source: Mapped[str] = mapped_column(String(64))           # e.g. greenhouse, remoteok
    source_name: Mapped[str] = mapped_column(String(128))     # human label
    # "company_ats" = pulled directly from the employer's own careers board API.
    # "aggregator"  = third-party job board; user should verify before applying.
    source_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(300), default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    salary: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(64), default="other", index=True)
    tags: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[str] = mapped_column(Text, default="")   # newline-separated bullets
    description: Mapped[str] = mapped_column(Text, default="")    # plain text, trimmed
    url: Mapped[str] = mapped_column(Text)                        # listing page
    apply_url: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SavedJob(Base):
    __tablename__ = "saved_jobs"
    __table_args__ = (UniqueConstraint("tg_id", "job_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SentAlert(Base):
    __tablename__ = "sent_alerts"
    __table_args__ = (UniqueConstraint("tg_id", "job_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    job_id: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_settings = get_settings()
if _settings.database_url.startswith("sqlite"):
    os.makedirs("data", exist_ok=True)
if _settings.database_url.startswith("sqlite"):
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(_settings.database_url, poolclass=NullPool)
else:
    engine = create_async_engine(_settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
