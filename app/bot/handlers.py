"""aiogram 3 handlers: user commands, profile editing, alerts, saved jobs and admin tools."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)

from ..classify import CATEGORY_LABELS, trim
from ..config import get_settings
from ..db import Job, SessionLocal
from .. import services as svc
from .format import channel_post, custom_post, job_card

log = logging.getLogger(__name__)
router = Router(name="main")
PAGE = 5


class ProfileForm(StatesGroup):
    skills = State()


class SearchForm(StatesGroup):
    query = State()


class AnnounceForm(StatesGroup):
    text = State()


def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in get_settings().admin_ids


def btn(text: str, data: str | None = None, url: str | None = None, web: str | None = None) -> InlineKeyboardButton:
    if web:
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=web))
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def app_button_row() -> list[InlineKeyboardButton]:
    url = get_settings().webapp_url
    return [btn("📱 Open Mini App", web=url)] if url.startswith("https://") else []


# ------------------------------------------------------------------ basics
HELP = (
    "<b>Web3 Job Finder</b>: real crypto jobs from company careers boards and trusted job sites.\n\n"
    "/jobs: browse the newest jobs by role\n"
    "/search &lt;words&gt;: search, e.g. <code>/search community manager</code>\n"
    "/remote: remote-only jobs\n"
    "/profile: set your roles and skills for matching\n"
    "/alerts: turn new-job alerts on or off\n"
    "/saved: your saved jobs\n"
    "/help: this message\n\n"
    "🏢 = straight from the company's own careers board.\n"
    "🔎 = third-party listing. Check it on the company's site.\n"
    "⚠️ Real employers never ask you to pay, buy tokens, or share a seed phrase to apply."
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject | None = None):
    await state.clear()
    from .. import accounts as acc
    if m.from_user is None or m.from_user.is_bot:
        return
    payload = (command.args or "") if command else ""
    tg = {"id": m.from_user.id, "username": m.from_user.username, "first_name": m.from_user.first_name,
          "is_bot": bool(m.from_user.is_bot)}
    async with SessionLocal() as s:
        u, is_new, outcome = await acc.upsert_user(s, tg, "bot", payload)
        code = await acc.ensure_code(s, u)
        await s.commit()
    if u.status == acc.SUSPENDED:
        await m.answer("Your access to this bot is suspended. Contact the bot admin if you think this is a mistake.")
        return
    if not acc.bot_username():
        try:
            acc.set_bot_username((await m.bot.me()).username)
        except Exception:
            pass
    links = acc.referral_links(code)
    name = escape(m.from_user.first_name or "there")
    welcome = "🎉 You joined with a friend's invite. Welcome!\n\n" if outcome == "valid" else ""
    invite = ""
    if links["bot_link"]:
        invite = ("\n\n🤝 <b>Invite friends</b>: share your personal link and earn reward points "
                  "(points only, no cash value):\n" + escape(links["app_link"] or links["bot_link"]))
    await m.answer(
        f"👋 Hi {name}! {welcome}I find <b>real Web3 jobs</b>: community, moderation, social media, support, "
        "BD, growth, developer and more.\n\nTap <b>Open Mini App</b> for the full app: job board, AI job agent, "
        "tracker and your profile. Or use /profile and /alerts right here." + invite + "\n\n" + HELP,
        reply_markup=kb(app_button_row(),
                        [btn("🎯 Jobs for me", "l|f||0"), btn("🆕 Latest", "l|a||0")],
                        [btn("👤 Set up profile", "p|show")]),
        disable_web_page_preview=True,
    )


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Your Telegram user ID: <code>{m.from_user.id}</code>\n"
                   "Put it in ADMIN_IDS on the server to unlock the owner tools (account login).")


@router.message(Command("help"))
async def cmd_help(m: Message):
    extra = ""
    if is_admin(m.from_user.id):
        extra = ("\n\n<b>Admin</b>\n/post &lt;job_id&gt;: publish a job to the channel\n"
                 "/announce: publish a custom announcement\n/refresh: fetch jobs now\n/stats: database stats")
    await m.answer(HELP + extra, reply_markup=kb(app_button_row()), disable_web_page_preview=True)


# ------------------------------------------------------------------ listing
def category_keyboard() -> InlineKeyboardMarkup:
    rows, row = [[btn("🎯 For me", "l|f||0"), btn("🆕 All latest", "l|a||0")]], []
    for key, label in CATEGORY_LABELS.items():
        row.append(btn(label, f"l|a|{key}|0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("jobs"))
async def cmd_jobs(m: Message):
    async with SessionLocal() as s:
        st = await svc.stats(s)
    if not st["total"]:
        await m.answer("⏳ I'm still collecting jobs from the sources. Try again in a minute.")
        return
    await m.answer(f"📋 <b>{st['total']}</b> live Web3 jobs. Pick a role:", reply_markup=category_keyboard())


@router.message(Command("remote"))
async def cmd_remote(m: Message, state: FSMContext):
    await send_page(m, m.from_user.id, "r", "", 0, state, edit=False)


@router.message(Command("search"))
async def cmd_search(m: Message, command: CommandObject, state: FSMContext):
    q = (command.args or "").strip()
    if not q:
        await state.set_state(SearchForm.query)
        await m.answer("🔍 What should I search for? e.g. <i>moderator</i>, <i>solidity</i>, <i>BD Asia</i>")
        return
    await state.update_data(q=q[:100])
    await send_page(m, m.from_user.id, "s", "", 0, state, edit=False)


@router.message(SearchForm.query, F.text, ~F.text.startswith("/"))
async def search_text(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        await m.answer("Search cancelled.")
        return
    await state.set_state(None)
    await state.update_data(q=m.text.strip()[:100])
    await send_page(m, m.from_user.id, "s", "", 0, state, edit=False)


@router.callback_query(F.data.startswith("l|"))
async def cb_list(c: CallbackQuery, state: FSMContext):
    _, mode, cat, off = c.data.split("|")
    await send_page(c.message, c.from_user.id, mode, cat, int(off), state, edit=True)
    await c.answer()


async def send_page(msg: Message, tg_id: int, mode: str, cat: str, offset: int, state: FSMContext, edit: bool):
    data = await state.get_data()
    async with SessionLocal() as s:
        scores: dict[int, int] = {}
        if mode == "f":
            user = await svc.get_or_create_user(s, tg_id)
            await s.commit()
            if not user.roles and not user.skills:
                text = "👤 Set your roles and skills first so I can match jobs to you."
                markup = kb([btn("👤 Set up profile", "p|show")])
                return await (msg.edit_text(text, reply_markup=markup) if edit else msg.answer(text, reply_markup=markup))
            matched = await svc.matched_jobs(s, user, limit=50)
            total = len(matched)
            page = matched[offset:offset + PAGE]
            jobs = [j for j, _ in page]
            scores = {j.id: sc for j, sc in page}
            title = "🎯 <b>Jobs matching your profile</b>"
        else:
            q = data.get("q", "") if mode == "s" else ""
            jobs, total = await svc.search_jobs(s, q=q, category=cat, remote=(mode == "r") or None,
                                                offset=offset, limit=PAGE)
            if mode == "s":
                title = f"🔍 Results for “{escape(q)}”"
            elif mode == "r":
                title = "🌍 <b>Remote Web3 jobs</b>"
            else:
                title = f"🆕 <b>{escape(CATEGORY_LABELS.get(cat, 'Latest Web3 jobs'))}</b>"
    if not jobs:
        text = title + "\n\nNo jobs found right now. Try another role or keyword, or turn on /alerts."
        markup = kb([btn("⬅️ Roles", "c|menu")])
    else:
        lines = [f"{title}  ({offset + 1}–{offset + len(jobs)} of {total})", ""]
        for i, j in enumerate(jobs, 1):
            badge = "🏢" if j.source_type == "company_ats" else "🔎"
            sal = f" · 💰 {escape(trim(j.salary, 40))}" if j.salary else ""
            sc = f" · 🎯{scores[j.id]}%" if j.id in scores else ""
            rem = " · 🌍" if j.remote else ""
            lines.append(f"<b>{i}.</b> {badge} <b>{escape(trim(j.title, 70))}</b>\n"
                         f"    {escape(trim(j.company, 40))}{rem}{sal}{sc}")
        text = "\n".join(lines)
        num_row = [btn(str(i), f"j|{j.id}") for i, j in enumerate(jobs, 1)]
        nav = []
        if offset > 0:
            nav.append(btn("◀️ Prev", f"l|{mode}|{cat}|{max(0, offset - PAGE)}"))
        if offset + PAGE < total:
            nav.append(btn("Next ▶️", f"l|{mode}|{cat}|{offset + PAGE}"))
        markup = kb(num_row, nav, [btn("⬅️ Roles", "c|menu")])
    if edit:
        try:
            await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "c|menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("Pick a role:", reply_markup=category_keyboard())
    await c.answer()


def job_keyboard(job: Job, saved: bool, admin: bool) -> InlineKeyboardMarkup:
    rows = [[btn("✅ Apply", url=job.apply_url), btn("🔗 Listing", url=job.url)],
            [btn("💔 Unsave" if saved else "⭐ Save", f"sv|{job.id}")]]
    if admin:
        rows.append([btn("📢 Post to channel", f"ap|{job.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("j|"))
async def cb_job(c: CallbackQuery):
    job_id = int(c.data.split("|")[1])
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        if not job:
            return await c.answer("This job is no longer listed.", show_alert=True)
        saved = job_id in await svc.saved_ids(s, c.from_user.id)
        user = await svc.get_or_create_user(s, c.from_user.id)
        score = svc.match_score(job, svc.split_csv(user.roles), svc.split_csv(user.skills), user.remote_only) \
            if (user.roles or user.skills) else None
        await s.commit()
    await c.message.answer(job_card(job, score, full=True),
                           reply_markup=job_keyboard(job, saved, is_admin(c.from_user.id)),
                           disable_web_page_preview=True)
    await c.answer()


@router.callback_query(F.data.startswith("sv|"))
async def cb_save(c: CallbackQuery):
    job_id = int(c.data.split("|")[1])
    async with SessionLocal() as s:
        try:
            state = await svc.toggle_save(s, c.from_user.id, job_id)
        except ValueError:
            return await c.answer("This job is no longer listed.", show_alert=True)
        await s.commit()
        job = await s.get(Job, job_id)
    await c.message.edit_reply_markup(reply_markup=job_keyboard(job, state, is_admin(c.from_user.id)))
    await c.answer("Saved ⭐" if state else "Removed")


@router.message(Command("saved"))
async def cmd_saved(m: Message):
    async with SessionLocal() as s:
        jobs = await svc.list_saved(s, m.from_user.id)
    if not jobs:
        return await m.answer("⭐ No saved jobs yet. Tap <b>Save</b> on any job to keep it here.",
                              reply_markup=kb(app_button_row()))
    lines = ["⭐ <b>Your saved jobs</b>", ""]
    for i, j in enumerate(jobs[:20], 1):
        lines.append(f"<b>{i}.</b> {escape(trim(j.title, 70))}: {escape(trim(j.company, 40))}")
    buttons = [btn(str(i), f"j|{j.id}") for i, j in enumerate(jobs[:20], 1)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows + [app_button_row()]))


# ------------------------------------------------------------------ profile
def roles_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, label in CATEGORY_LABELS.items():
        row.append(btn(("✅ " if key in selected else "") + label, f"pr|{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("💾 Done", "p|show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_profile(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, tg_id)
        await s.commit()
    roles = [CATEGORY_LABELS.get(r, r) for r in svc.split_csv(u.roles)]
    text = ("👤 <b>Your job profile</b>\n\n"
            f"<b>Roles:</b> {escape(', '.join(roles)) or 'not set'}\n"
            f"<b>Skills:</b> {escape(u.skills.replace(',', ', ')) or 'not set'}\n"
            f"<b>Remote only:</b> {'Yes' if u.remote_only else 'No'}\n"
            f"<b>Alerts:</b> {'On 🔔' if u.alerts_enabled else 'Off 🔕'}")
    markup = kb([btn("💼 Choose roles", "p|roles"), btn("🛠 Set skills", "p|skills")],
                [btn("🌍 Remote only: " + ("ON" if u.remote_only else "OFF"), "p|remote")],
                [btn("🎯 See my matches", "l|f||0")], app_button_row())
    return text, markup


@router.message(Command("profile"))
async def cmd_profile(m: Message, state: FSMContext):
    await state.set_state(None)
    async with SessionLocal() as s:
        await svc.get_or_create_user(s, m.from_user.id, m.from_user.username, m.from_user.first_name)
        await s.commit()
    text, markup = await render_profile(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("p|"))
async def cb_profile(c: CallbackQuery, state: FSMContext):
    action = c.data.split("|")[1]
    if action == "roles":
        async with SessionLocal() as s:
            u = await svc.get_or_create_user(s, c.from_user.id)
            await s.commit()
        await c.message.edit_text("💼 Tap the roles you want (tap again to remove):",
                                  reply_markup=roles_keyboard(svc.split_csv(u.roles)))
    elif action == "skills":
        await state.set_state(ProfileForm.skills)
        await c.message.answer("🛠 Send your skills separated by commas, e.g.\n"
                               "<code>discord, telegram, content writing, english, solidity</code>")
    elif action == "remote":
        async with SessionLocal() as s:
            u = await svc.get_or_create_user(s, c.from_user.id)
            u.remote_only = not u.remote_only
            await s.commit()
        text, markup = await render_profile(c.from_user.id)
        await c.message.edit_text(text, reply_markup=markup)
    else:
        text, markup = await render_profile(c.from_user.id)
        try:
            await c.message.edit_text(text, reply_markup=markup)
        except Exception:
            await c.message.answer(text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith("pr|"))
async def cb_role(c: CallbackQuery):
    key = c.data.split("|")[1]
    if key not in CATEGORY_LABELS:
        return await c.answer()
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, c.from_user.id)
        roles = svc.split_csv(u.roles)
        roles = [r for r in roles if r != key] if key in roles else roles + [key]
        u.roles = ",".join(roles)
        await s.commit()
    await c.message.edit_reply_markup(reply_markup=roles_keyboard(roles))
    await c.answer()


@router.message(ProfileForm.skills, F.text, ~F.text.startswith("/"))
async def skills_text(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return await m.answer("Cancelled.")
    skills = [trim(x.strip().lower(), 40) for x in m.text.replace("\n", ",").split(",") if x.strip()][:20]
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, m.from_user.id)
        u.skills = ",".join(skills)
        await s.commit()
    await state.clear()
    text, markup = await render_profile(m.from_user.id)
    await m.answer("✅ Skills saved.\n\n" + text, reply_markup=markup)


# ------------------------------------------------------------------ alerts
@router.message(Command("alerts"))
async def cmd_alerts(m: Message):
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, m.from_user.id, m.from_user.username, m.from_user.first_name)
        await s.commit()
    await m.answer(alerts_text(u.alerts_enabled, bool(u.roles or u.skills)), reply_markup=alerts_kb(u.alerts_enabled))


def alerts_text(on: bool, has_profile: bool) -> str:
    t = ("🔔 <b>Job alerts are ON.</b> I'll message you when new jobs match your profile (max 5 per check)."
         if on else "🔕 <b>Job alerts are OFF.</b> Turn them on to get new matching jobs in this chat.")
    if not has_profile:
        t += "\n\n⚠️ Set your roles or skills in /profile first. Alerts only send matching jobs."
    return t


def alerts_kb(on: bool) -> InlineKeyboardMarkup:
    return kb([btn("🔕 Turn off" if on else "🔔 Turn on alerts", "al|toggle")], [btn("👤 Profile", "p|show")])


@router.callback_query(F.data == "al|toggle")
async def cb_alerts(c: CallbackQuery):
    async with SessionLocal() as s:
        u = await svc.get_or_create_user(s, c.from_user.id)
        u.alerts_enabled = not u.alerts_enabled
        on, has_profile = u.alerts_enabled, bool(u.roles or u.skills)
        await s.commit()
    await c.message.edit_text(alerts_text(on, has_profile), reply_markup=alerts_kb(on))
    await c.answer("Alerts on 🔔" if on else "Alerts off")


async def deliver_alerts(bot: Bot, new_ids: list[int]) -> int:
    from ..db import SentAlert
    import asyncio
    sent = 0
    async with SessionLocal() as s:
        plan = await svc.pending_alerts(s, new_ids)
        for tg_id, jobs in plan.items():
            for job in jobs:
                try:
                    await bot.send_message(tg_id, "🔔 <b>New matching job</b>\n\n" + job_card(job),
                                           reply_markup=job_keyboard(job, False, False),
                                           disable_web_page_preview=True)
                    s.add(SentAlert(tg_id=tg_id, job_id=job.id))
                    sent += 1
                except Exception as e:  # user blocked the bot, etc.
                    log.info("alert to %s failed: %s", tg_id, e)
                    if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                        u = await svc.get_or_create_user(s, tg_id)
                        u.alerts_enabled = False
                    break
                await asyncio.sleep(0.05)  # stay well under Telegram rate limits
        await s.commit()
    return sent


# ------------------------------------------------------------------ admin
@router.message(Command("post"))
async def cmd_post(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admins only.")
    if not get_settings().channel_id:
        return await m.answer("CHANNEL_ID is not set in the environment.")
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer("Usage: <code>/post 123</code> (job ID from the Mini App or a job card's 📢 button)")
    await preview_job_post(m, int(arg))


@router.callback_query(F.data.startswith("ap|"))
async def cb_admin_post(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await preview_job_post(c.message, int(c.data.split("|")[1]))
    await c.answer()


async def preview_job_post(msg: Message, job_id: int):
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
    if not job:
        return await msg.answer("Job not found.")
    await msg.answer("👀 <b>Preview</b>. This goes to the channel:\n\n" + channel_post(job),
                     reply_markup=kb([btn("✅ Publish", f"pub|j|{job_id}"), btn("✖️ Cancel", "pub|x|0")]),
                     disable_web_page_preview=True)


@router.message(Command("announce"))
async def cmd_announce(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admins only.")
    await state.set_state(AnnounceForm.text)
    await m.answer("📝 Send the announcement in this format (one item per line):\n\n"
                   "<code>Title\nCompany\nhttps://apply-link\nDescription text…</code>\n\nSend /cancel to stop.")


@router.message(AnnounceForm.text, F.text, ~F.text.startswith("/"))
async def announce_text(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    if m.text.startswith("/"):
        await state.clear()
        return await m.answer("Cancelled.")
    parts = m.text.split("\n", 3)
    if len(parts) < 4 or not parts[2].strip().startswith("https://"):
        return await m.answer("Please send 4 parts: title, company, an https:// link, then the description.")
    title, company, link, body = (p.strip() for p in parts)
    await state.update_data(ann=dict(title=title[:200], company=company[:200], link=link[:500], body=body[:3000]))
    await state.set_state(None)
    await m.answer("👀 <b>Preview</b>:\n\n" + custom_post(title, company, body, link),
                   reply_markup=kb([btn("✅ Publish", "pub|a|0"), btn("✖️ Cancel", "pub|x|0")]),
                   disable_web_page_preview=True)


@router.callback_query(F.data.startswith("pub|"))
async def cb_publish(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(c.from_user.id):  # re-checked at publish time, not only at preview
        return await c.answer("Admins only.", show_alert=True)
    _, kind, ref = c.data.split("|")
    if kind == "x":
        await c.message.edit_text("Cancelled.")
        return await c.answer()
    channel = get_settings().channel_id
    if kind == "j":
        async with SessionLocal() as s:
            job = await s.get(Job, int(ref))
        if not job:
            return await c.answer("Job not found.", show_alert=True)
        text = channel_post(job)
    else:
        ann = (await state.get_data()).get("ann")
        if not ann:
            return await c.answer("Announcement expired. Send /announce again.", show_alert=True)
        text = custom_post(ann["title"], ann["company"], ann["body"], ann["link"])
    try:
        await bot.send_message(channel, text, disable_web_page_preview=True)
    except Exception as e:
        return await c.message.answer(f"❌ Could not post: {escape(str(e))}\n"
                                      "Make sure the bot is an admin of the channel with 'Post messages'.")
    log.info("admin %s published %s:%s to %s", c.from_user.id, kind, ref, channel)
    await c.message.edit_text("✅ Published to the channel.")
    await c.answer()


@router.message(Command("refresh"))
async def cmd_refresh(m: Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admins only.")
    await m.answer("⏳ Fetching jobs from all sources…")
    new_ids, status = await svc.refresh_jobs()
    sent = await deliver_alerts(bot, new_ids)
    lines = "\n".join(f"• {escape(k)}: {escape(v)}" for k, v in status.items())
    await m.answer(f"✅ Done. {len(new_ids)} new jobs, {sent} alerts sent.\n\n{lines}")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admins only.")
    async with SessionLocal() as s:
        st = await svc.stats(s)
    lines = "\n".join(f"• {escape(CATEGORY_LABELS.get(k, k))}: {v}" for k, v in sorted(st["by_category"].items()))
    await m.answer(f"📊 <b>{st['total']}</b> jobs\n{lines}")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.")
