import asyncio
import hashlib
import html
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message
)
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web3-job-finder")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "900"))

# Configure public RSS feeds you are permitted to use.
RSS_FEEDS = [
    ("Google News · Web3 jobs", "https://news.google.com/rss/search?q=web3+jobs+remote&hl=en-US&gl=US&ceid=US:en"),
    ("Google News · Crypto community jobs", "https://news.google.com/rss/search?q=crypto+community+manager+jobs+remote&hl=en-US&gl=US&ceid=US:en"),
    ("Google News · Blockchain jobs", "https://news.google.com/rss/search?q=blockchain+jobs+remote&hl=en-US&gl=US&ceid=US:en"),
]
ROLE_TERMS = [
    ("Community Manager", ["community manager", "community lead", "community specialist"]),
    ("Moderator", ["moderator", "discord mod", "telegram mod", "community moderator"]),
    ("Social Media Manager", ["social media manager", "social media", "content manager"]),
    ("Customer Support", ["customer support", "support specialist", "customer success"]),
    ("Business Development", ["business development", "partnerships", "bd manager", "growth manager"]),
    ("Developer", ["developer", "engineer", "solidity", "frontend", "backend"]),
    ("Marketing", ["marketing", "growth marketer", "content creator", "k ol", "influencer"]),
]
ALLOWED_SCHEMES = {"http", "https"}
jobs = []
seen_ids = set()
subscribers = set()
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

DEMO_JOBS = [
    {
        "id": "demo-community",
        "title": "Web3 Community Moderator (Demo listing)",
        "company": "Demo listing — replace with verified source",
        "role": "Moderator",
        "location": "Remote",
        "salary_min": 100,
        "salary_label": "$100–$500/mo (example only)",
        "description": "This is sample data to demonstrate the Mini App. Do not apply; replace with a real verified listing.",
        "url": "https://example.com/",
        "source_name": "Demo",
        "posted_label": "Demo data",
        "verified": False,
    }
]

def classify_role(title, summary):
    text = f"{title} {summary}".lower()
    for role, terms in ROLE_TERMS:
        if any(term in text for term in terms):
            return role
    return "Other"

def clean_html(raw):
    raw = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()[:500]

def salary_from_text(text):
    # Conservative: extract a plausible monthly USD lower bound only when clearly stated.
    matches = re.findall(r"\$\s?([\d,]+)\s*(?:/|per\s+)?\s*(month|mo|monthly)", text.lower())
    if matches:
        try:
            return int(matches[0][0].replace(",", ""))
        except ValueError:
            pass
    return 0

def normalize_entry(entry, source_name):
    title = clean_html(entry.get("title", "Web3 opportunity"))
    summary = clean_html(entry.get("summary", entry.get("description", "")))
    url = entry.get("link", "")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        return None
    published = entry.get("published", "")
    role = classify_role(title, summary)
    identity = entry.get("id") or url
    key = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()[:20]
    return {
        "id": key,
        "title": title[:180],
        "company": source_name,
        "role": role,
        "location": "Remote / check listing",
        "salary_min": salary_from_text(f"{title} {summary}"),
        "salary_label": "Salary not listed" if not salary_from_text(f"{title} {summary}") else f"From ${salary_from_text(f'{title} {summary}')}/mo (source text)",
        "description": summary,
        "url": url,
        "source_name": source_name,
        "posted_label": published[:60] if published else "Recently listed",
        "verified": False,
    }

async def fetch_jobs():
    global jobs
    found = []
    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)
            for entry in feed.entries[:30]:
                item = normalize_entry(entry, source_name)
                if item:
                    found.append(item)
        except Exception as exc:
            log.warning("Feed fetch failed for %s: %s", source_name, exc)
    # Deduplicate by canonical URL/id and keep newest feed results first.
    unique, ids = [], set()
    for item in found:
        if item["id"] not in ids:
            ids.add(item["id"])
            unique.append(item)
    if unique:
        jobs = unique[:100]
    elif not jobs:
        jobs = DEMO_JOBS.copy()
    return jobs

async def alert_loop():
    while True:
        try:
            previous = set(seen_ids)
            current = await fetch_jobs()
            new_items = [j for j in current if j["id"] not in previous and not j["id"].startswith("demo-")]
            seen_ids.update(j["id"] for j in current)
            if bot and subscribers and new_items:
                for user_id in list(subscribers):
                    for item in new_items[:5]:
                        msg = (
                            f"🆕 <b>{html.escape(item['title'])}</b>\n"
                            f"Role: {html.escape(item['role'])}\n"
                            f"Source: {html.escape(item['source_name'])}\n"
                            f"Salary: {html.escape(item['salary_label'])}\n"
                            f"<a href=\"{html.escape(item['url'], quote=True)}\">Open listing</a>\n\n"
                            "Please verify the employer and never pay a recruitment fee."
                        )
                        try:
                            await bot.send_message(user_id, msg, parse_mode="HTML", disable_web_page_preview=True)
                        except Exception as exc:
                            log.info("Could not send alert to %s: %s", user_id, exc)
        except Exception:
            log.exception("Alert loop failed")
        await asyncio.sleep(max(60, ALERT_INTERVAL_SECONDS))

@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    subscribers.add(user_id)
    keyboard = None
    if WEBAPP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Open Job Finder", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
    text = (
        "Welcome to <b>AI Web3 Job Finder</b>.\n\n"
        "I collect public Web3 job posts and can send you new-job alerts. "
        "Listings are not guaranteed legitimate, so verify each employer before applying.\n\n"
        "You are subscribed to alerts. Use /alerts_off to stop them."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("jobs"))
async def cmd_jobs(message: Message):
    await fetch_jobs()
    if not jobs:
        await message.answer("No listings found right now. Try again later.")
        return
    lines = []
    for j in jobs[:8]:
        lines.append(f"• <b>{html.escape(j['title'])}</b>\n  {html.escape(j['role'])} · {html.escape(j['salary_label'])}\n  <a href=\"{html.escape(j['url'], quote=True)}\">Open source</a>")
    await message.answer("\n\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("alerts_on"))
async def alerts_on(message: Message):
    subscribers.add(message.from_user.id)
    await message.answer("Job alerts enabled. Use /alerts_off to unsubscribe.")

@dp.message(Command("alerts_off"))
async def alerts_off(message: Message):
    subscribers.discard(message.from_user.id)
    await message.answer("Job alerts disabled. You can use /alerts_on any time.")

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "/start — subscribe and open the Mini App\n"
        "/jobs — show latest listings\n"
        "/alerts_on — enable alerts\n"
        "/alerts_off — stop alerts\n\n"
        "Safety: never pay to get hired or share your wallet seed phrase."
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(alert_loop())
    if bot:
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Start job finder"),
                BotCommand(command="jobs", description="Latest jobs"),
                BotCommand(command="alerts_on", description="Enable job alerts"),
                BotCommand(command="alerts_off", description="Disable job alerts"),
                BotCommand(command="help", description="Help and safety"),
            ])
        except Exception as exc:
            log.warning("Could not set bot commands: %s", exc)
    yield
    task.cancel()
    if bot:
        await bot.session.close()

app = FastAPI(title="AI Web3 Job Finder", lifespan=lifespan)

@app.get("/api/jobs")
async def api_jobs():
    await fetch_jobs()
    return JSONResponse({"jobs": jobs, "updated_at": datetime.now(timezone.utc).isoformat()})

@app.get("/health")
async def health():
    return {"ok": True, "bot_configured": bool(BOT_TOKEN), "webapp_url_configured": WEBAPP_URL.startswith("https://")}

# Serve the Telegram Mini App from the same HTTPS origin as /api/jobs.
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")

async def run_bot():
    if not bot or not BOT_TOKEN:
        log.warning("BOT_TOKEN is missing. API will run, but Telegram bot polling is disabled.")
        return
    await dp.start_polling(bot)

async def main():
    api_config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(api_config)
    await asyncio.gather(server.serve(), run_bot())

if __name__ == "__main__":
    asyncio.run(main())
