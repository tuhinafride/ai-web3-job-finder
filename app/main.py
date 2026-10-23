"""FastAPI app: Mini App API + static Mini App + Telegram webhook + background job fetcher.

Run locally:   uvicorn app.main:app --reload        (set BOT_MODE=polling for local testing)
On Render:     uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, Update, WebAppInfo
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import services as svc
from .bot.handlers import deliver_alerts, router
from .classify import CATEGORY_LABELS
from .config import get_settings
from .db import Job, SessionLocal, init_db
from . import userclient as uc
from .outreach import models as _outreach_models  # noqa: F401  (registers tables)
from .outreach import agent as outreach_agent
from .outreach.api import router as agent_router
from . import accounts as acc
from .admin_api import router as admin_router
from .auth import current_user, ip_bucket


class RedactTokenFilter(logging.Filter):
    """Never let the bot token reach the logs (aiogram/httpx can log request URLs)."""
    def filter(self, record: logging.LogRecord) -> bool:
        token = get_settings().bot_token
        if token:
            msg = record.getMessage()
            if token in msg:
                record.msg = msg.replace(token, "***REDACTED***")
                record.args = ()
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for h in logging.getLogger().handlers:
    h.addFilter(RedactTokenFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

settings = get_settings()
bot: Bot | None = None
dp = Dispatcher()
dp.include_router(router)
_tasks: list[asyncio.Task] = []

COMMANDS = [
    BotCommand(command="start", description="Welcome & menu"),
    BotCommand(command="jobs", description="Browse latest Web3 jobs"),
    BotCommand(command="search", description="Search jobs"),
    BotCommand(command="remote", description="Remote jobs"),
    BotCommand(command="alerts", description="Job alerts on/off"),
    BotCommand(command="saved", description="Saved jobs"),
    BotCommand(command="profile", description="Your roles & skills"),
    BotCommand(command="help", description="Help"),
]


async def refresh_loop():
    await asyncio.sleep(3)
    while True:
        try:
            new_ids, _ = await svc.refresh_jobs()
            if bot:
                sent = await deliver_alerts(bot, new_ids)
                log.info("alerts sent: %d", sent)
        except Exception:
            log.exception("refresh failed")
        await asyncio.sleep(max(15, settings.fetch_interval_minutes) * 60)


async def outreach_loop():
    """Sends queued approved drafts (respecting limits) and runs optional auto-searches."""
    from sqlalchemy import select
    from datetime import timedelta
    from .db import utcnow
    await asyncio.sleep(10)
    while True:
        try:
            await outreach_agent.sender_tick()
            async with SessionLocal() as s:
                states = (await s.scalars(select(_outreach_models.AgentState).where(
                    _outreach_models.AgentState.auto_search_hours > 0,
                    _outreach_models.AgentState.stopped.is_(False)))).all()
            for st in states:
                due = not st.last_search_at or utcnow() - st.last_search_at >= timedelta(hours=st.auto_search_hours)
                if due and st.owner_id in settings.admin_ids:
                    outreach_agent.start_discovery(st.owner_id, ["google", "x", "telegram"])
        except Exception:
            log.exception("outreach loop error")
        await asyncio.sleep(30)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global bot
    await init_db()
    if settings.bot_token:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await setup_bot(bot)
        except Exception as e:  # never crash the web app because Telegram is unreachable
            log.error("Telegram setup failed: %s", type(e).__name__)
    else:
        log.warning("BOT_TOKEN not set: API/Mini App run, Telegram bot disabled")
    if not settings.disable_scheduler:
        _tasks.append(asyncio.create_task(refresh_loop()))
        _tasks.append(asyncio.create_task(outreach_loop()))
    yield
    for t in _tasks:
        t.cancel()
    if bot:
        await bot.session.close()


BOT_INFO: dict = {"username": ""}


async def setup_bot(bot: Bot):
    if settings.bot_mode not in ("webhook", "polling"):
        return
    try:
        BOT_INFO["username"] = (await bot.get_me()).username or ""
        acc.set_bot_username(BOT_INFO["username"])
    except Exception:
        pass
    await bot.set_my_commands(COMMANDS)
    if settings.webapp_url.startswith("https://"):
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Jobs App", web_app=WebAppInfo(url=settings.webapp_url)))
    if settings.bot_mode == "polling":
        await bot.delete_webhook(drop_pending_updates=False)
        _tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
        log.info("bot started in polling mode")
    else:
        if not (settings.public_base_url and settings.webhook_secret):
            log.error("BOT_MODE=webhook needs PUBLIC_BASE_URL and WEBHOOK_SECRET")
        else:
            await bot.set_webhook(f"{settings.public_base_url.rstrip('/')}/telegram/webhook",
                                  secret_token=settings.webhook_secret,
                                  allowed_updates=dp.resolve_used_update_types(),
                                  drop_pending_updates=False)
            log.info("webhook registered")


app = FastAPI(title="Web3 Job Finder", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(agent_router)
app.include_router(admin_router)


def _client_ip(request: Request) -> str:
    # Behind Render's proxy request.client is the proxy, so use the client IP from X-Forwarded-For.
    # (Spoofable, but it only keys the coarse IP bucket; per-user limits need valid initData.)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_and_headers(request: Request, call_next):
    if request.url.path.startswith("/api/") and not ip_bucket.allow(f"ip:{_client_ip(request)}"):
        return JSONResponse({"detail": "Too many requests. Slow down a little."}, status_code=429)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(acc.LimitReached)
async def limit_reached(_: Request, exc: acc.LimitReached):
    what = "searches" if exc.kind == "search" else "message drafts"
    return JSONResponse({"detail": f"Daily limit reached: {exc.limit} {what} per day. It resets at 00:00 UTC.",
                         "code": "daily_limit", "kind": exc.kind}, status_code=429)


# ------------------------------------------------------------------ webhook
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.webhook_secret or not hmac.compare_digest(secret, settings.webhook_secret):
        raise HTTPException(status_code=403)
    if bot is None:
        raise HTTPException(status_code=503)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/health")
async def health():
    async with SessionLocal() as s:
        st = await svc.stats(s)
    return {"status": "ok", "jobs": st["total"]}


@app.get("/api/botinfo")
async def botinfo():
    """Public: lets the Mini App tell the user which bot it belongs to."""
    return {"username": acc.bot_username()}


@app.get("/")
async def root():
    return RedirectResponse("/app/")


# ------------------------------------------------------------------ Mini App API
@app.get("/api/meta")
async def api_meta(user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        st = await svc.stats(s)
    return {"categories": CATEGORY_LABELS, "stats": st,
            "is_admin": user["is_admin"], "user_id": user["id"]}


# ------------------------------------------------------------------ me / referrals
@app.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    from .db import User
    async with SessionLocal() as s:
        u = await s.get(User, user["id"])
        code = await acc.ensure_code(s, u)
        stats = await acc.referral_stats(s, u.tg_id)
        cfg = await acc.referral_config(s)
        limits = await acc.limits_for(s, u.tg_id)
        await s.commit()
    return {
        # True only because this request carried initData that passed the server-side HMAC check.
        "connected": True, "id": u.tg_id, "username": u.username, "first_name": u.first_name,
        "is_admin": user["is_admin"], "status": u.status or "active",
        "registered_at": acc.iso(u.registered_at or u.created_at), "referred": bool(u.referred_by),
        "referral": {"code": code, **acc.referral_links(code), **stats,
                     "points_enabled": cfg["enabled"], "points_per_referral": cfg["points_per_referral"],
                     "points_label": cfg["label"]},
        "limits": limits,
    }


@app.get("/api/referrals/leaderboard")
async def api_leaderboard(user: dict = Depends(current_user), limit: int = Query(10, ge=1, le=50)):
    async with SessionLocal() as s:
        rows = await acc.leaderboard(s, limit, include_ids=user["is_admin"])
        cfg = await acc.referral_config(s)
    return {"leaderboard": rows, "points_label": cfg["label"]}


@app.get("/api/jobs")
async def api_jobs(user: dict = Depends(current_user), q: str = Query("", max_length=100),
                   category: str = Query("", max_length=32), remote: bool = False,
                   has_salary: bool = False, verified_only: bool = False, for_me: bool = False,
                   offset: int = Query(0, ge=0, le=5000), limit: int = Query(20, ge=1, le=50)):
    async with SessionLocal() as s:
        saved = await svc.saved_ids(s, user["id"])
        if for_me:
            u = await svc.get_or_create_user(s, user["id"], user.get("username"), user.get("first_name"))
            await s.commit()
            matched = await svc.matched_jobs(s, u, limit=200)
            if q:
                ql = q.lower()
                matched = [m for m in matched if ql in f"{m[0].title} {m[0].company} {m[0].tags}".lower()]
            if remote:
                matched = [m for m in matched if m[0].remote]
            if has_salary:
                matched = [m for m in matched if m[0].salary]
            if verified_only:
                matched = [m for m in matched if m[0].source_type == "company_ats"]
            page = matched[offset:offset + limit]
            return {"total": len(matched), "jobs": [svc.job_dict(j, j.id in saved, sc) for j, sc in page]}
        jobs, total = await svc.search_jobs(s, q=q, category=category, remote=remote or None,
                                            has_salary=has_salary, verified_only=verified_only,
                                            offset=offset, limit=limit)
        return {"total": total, "jobs": [svc.job_dict(j, j.id in saved) for j in jobs]}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int, user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        u = await svc.get_or_create_user(s, user["id"])
        await s.commit()
        score = svc.match_score(job, svc.split_csv(u.roles), svc.split_csv(u.skills), u.remote_only) \
            if (u.roles or u.skills) else None
        return svc.job_dict(job, job_id in await svc.saved_ids(s, user["id"]), score)


@app.get("/api/saved")
async def api_saved(user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        return {"jobs": [svc.job_dict(j, True) for j in await svc.list_saved(s, user["id"])]}


@app.post("/api/saved/{job_id}")
async def api_save(job_id: int, user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        try:
            await svc.toggle_save(s, user["id"], job_id, want=True)
        except ValueError:
            raise HTTPException(404, "Job not found")
        await s.commit()
    return {"saved": True}


@app.delete("/api/saved/{job_id}")
async def api_unsave(job_id: int, user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        await svc.toggle_save(s, user["id"], job_id, want=False)
        await s.commit()
    return {"saved": False}


class ProfileIn(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=12)
    skills: list[str] = Field(default_factory=list, max_length=20)
    remote_only: bool = False
    alerts_enabled: bool = False


@app.get("/api/profile")
async def api_profile(user: dict = Depends(current_user)):
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, user["id"], user.get("username"), user.get("first_name"))
        await s.commit()
        return svc.user_profile_dict(u)


@app.put("/api/profile")
async def api_profile_put(body: ProfileIn, user: dict = Depends(current_user)):
    roles = [r for r in body.roles if r in CATEGORY_LABELS]
    skills = [s.strip().lower().replace(",", " ")[:40] for s in body.skills if s.strip()]
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, user["id"], user.get("username"), user.get("first_name"))
        u.roles, u.skills = ",".join(roles), ",".join(skills)
        u.remote_only, u.alerts_enabled = body.remote_only, body.alerts_enabled
        await s.commit()
        return svc.user_profile_dict(u)


# ------------------------------------------------------------------ personal account (owner only)
async def owner_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail=f"owner_only:{user['id']}")
    return user


def _login_error(e: "uc.LoginError"):
    return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.status)


class ApiCredsIn(BaseModel):
    api_id: str = Field(max_length=16)
    api_hash: str = Field(max_length=64)


class PhoneIn(BaseModel):
    phone: str = Field(max_length=32)


class CodeIn(BaseModel):
    code: str = Field(max_length=16)


class PasswordIn(BaseModel):
    password: str = Field(max_length=256)


class DisconnectIn(BaseModel):
    forget_api: bool = False


@app.get("/api/account/status")
async def account_status(user: dict = Depends(owner_user)):
    return await uc.status(user["id"])


@app.post("/api/account/api-credentials")
async def account_api_creds(body: ApiCredsIn, user: dict = Depends(owner_user)):
    try:
        await uc.save_api_credentials(user["id"], body.api_id, body.api_hash)
    except uc.LoginError as e:
        return _login_error(e)
    return await uc.status(user["id"])


@app.post("/api/account/send-code")
async def account_send_code(body: PhoneIn, user: dict = Depends(owner_user)):
    try:
        return await uc.send_code(user["id"], body.phone)
    except uc.LoginError as e:
        return _login_error(e)


@app.post("/api/account/code")
async def account_code(body: CodeIn, user: dict = Depends(owner_user)):
    try:
        return await uc.submit_code(user["id"], body.code)
    except uc.LoginError as e:
        return _login_error(e)


@app.post("/api/account/password")
async def account_password(body: PasswordIn, user: dict = Depends(owner_user)):
    try:
        return await uc.submit_password(user["id"], body.password)
    except uc.LoginError as e:
        return _login_error(e)


@app.post("/api/account/cancel")
async def account_cancel(user: dict = Depends(owner_user)):
    await uc.cancel(user["id"])
    return await uc.status(user["id"])


@app.post("/api/account/verify")
async def account_verify(user: dict = Depends(owner_user)):
    try:
        return await uc.verify_live(user["id"])
    except uc.LoginError as e:
        return _login_error(e)


@app.post("/api/account/disconnect")
async def account_disconnect(body: DisconnectIn, user: dict = Depends(owner_user)):
    try:
        return await uc.disconnect(user["id"], body.forget_api)
    except uc.LoginError as e:
        return _login_error(e)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error: %s", exc)
    return JSONResponse({"detail": "Internal error"}, status_code=500)


WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
app.mount("/app", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
