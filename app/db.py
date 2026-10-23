"""Database models and session helpers (SQLite by default, PostgreSQL in production)."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import logging

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, inspect, text)
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
    # --- multi-user / referral columns (added by an idempotent startup migration on old DBs) ---
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_app_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # first Mini App open
    status: Mapped[str | None] = mapped_column(String(16), default="active", nullable=True)  # active|suspended
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True, nullable=True)
    referral_source: Mapped[str | None] = mapped_column(String(64), nullable=True)  # referral|bot|miniapp|legacy


class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, unique=True)   # one referrer per user, ever
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    points: Mapped[int] = mapped_column(Integer, default=0)            # reward points (no money value)
    channel: Mapped[str] = mapped_column(String(16), default="")         # bot | miniapp


class ReferralEvent(Base):
    __tablename__ = "referral_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(String(24))     # link_created | attributed | rejected
    details: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class UserActivity(Base):
    __tablename__ = "user_activity"
    __table_args__ = (UniqueConstraint("user_id", "day", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="app")   # app | bot


class UsageCounter(Base):
    """Per-user daily counters for paid provider calls (searches, AI drafts)."""
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("user_id", "day", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


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


log = logging.getLogger("db")

# Columns added to tables that already exist in production. create_all() never alters an
# existing table, so these are added here. Only ADD COLUMN: nothing is dropped or rewritten.
MIGRATION_COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    # table: [(column, postgres type, sqlite type)]
    "users": [
        ("username", "VARCHAR(64)", "VARCHAR(64)"),
        ("first_name", "VARCHAR(128)", "VARCHAR(128)"),
        ("registered_at", "TIMESTAMP WITHOUT TIME ZONE", "DATETIME"),
        ("last_active_at", "TIMESTAMP WITHOUT TIME ZONE", "DATETIME"),
        ("opened_app_at", "TIMESTAMP WITHOUT TIME ZONE", "DATETIME"),
        ("status", "VARCHAR(16) DEFAULT 'active'", "VARCHAR(16) DEFAULT 'active'"),
        ("referred_by", "BIGINT", "BIGINT"),
        ("referral_code", "VARCHAR(16)", "VARCHAR(16)"),
        ("referral_source", "VARCHAR(64)", "VARCHAR(64)"),
    ],
}
MIGRATION_SQL_AFTER = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code ON users (referral_code)",
    "CREATE INDEX IF NOT EXISTS ix_users_referred_by ON users (referred_by)",
    "UPDATE users SET registered_at = created_at WHERE registered_at IS NULL",
    "UPDATE users SET status = 'active' WHERE status IS NULL",
    "UPDATE users SET referral_source = 'legacy' WHERE referral_source IS NULL",
]


def migration_statements(dialect: str, existing: dict[str, set[str]]) -> list[str]:
    """SQL needed to bring old tables up to date. Pure function so it can be tested per dialect."""
    out: list[str] = []
    for table, cols in MIGRATION_COLUMNS.items():
        have = existing.get(table)
        if have is None:        # table was just created by create_all with every column
            continue
        for name, pg_type, lite_type in cols:
            if dialect == "postgresql":
                out.append(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {pg_type}")
            elif name not in have:          # sqlite has no IF NOT EXISTS for columns
                out.append(f"ALTER TABLE {table} ADD COLUMN {name} {lite_type}")
    return out + MIGRATION_SQL_AFTER


def _existing_columns(sync_conn) -> dict[str, set[str]]:
    insp = inspect(sync_conn)
    names = set(insp.get_table_names())
    return {t: {c["name"] for c in insp.get_columns(t)} for t in MIGRATION_COLUMNS if t in names}


async def migrate(conn) -> list[str]:
    existing = await conn.run_sync(_existing_columns)
    stmts = migration_statements(conn.dialect.name, existing)
    for sql in stmts:
        await conn.execute(text(sql))
    added = [x for x in stmts if x.startswith("ALTER")]
    if added and conn.dialect.name != "postgresql":
        log.info("migration added %d column(s)", len(added))
    return stmts


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await migrate(conn)
