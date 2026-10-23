# Web3 Job Finder: Telegram Bot + Mini App

A Telegram bot and Mini App that finds **real** Web3 / crypto jobs (Community Manager, Moderator,
Social Media, Customer Support, BD, Growth, Developer and more), matches them to each user's
roles and skills, sends opt-in alerts, and lets admins publish formatted posts to a channel.

Built with **Python 3.12, aiogram 3, FastAPI, SQLAlchemy (SQLite or PostgreSQL)**.

---

## 1. Where the jobs come from (no fake listings)

The bot only shows jobs it downloaded from public job feeds. It never invents a listing.

| Label in the bot | Meaning | Sources |
|---|---|---|
| 🏢 **Company board** | Pulled straight from the employer's own careers system | Greenhouse: Consensys/MetaMask, Coinbase, Gemini, Ripple, Fireblocks, OKX, BitGo · Lever: Binance, 1inch · Ashby: Kraken, Uniswap Labs, Phantom, Polygon Labs, Paradigm portfolio |
| 🔎 **Third-party** | Posted on a job board. The user is told to verify it on the company site | Remote OK, Remotive (crypto-filtered), Cryptocurrency Jobs |

Neither label says a job is "legit". It only says where it came from. Every job shows its
source name and links to the original listing. Salary appears only when the source or the
employer's own description lists it. Jobs are refreshed every 60 minutes and removed after
they disappear from their source for 45 days.

**Add more companies:** edit the lists at the top of `app/sources/providers.py`. The slug is in
the company's careers URL, e.g. `job-boards.greenhouse.io/`**`consensys`**, `jobs.lever.co/`**`binance`**,
`jobs.ashbyhq.com/`**`phantom`**.

---

## 2. Features

**Bot commands**

| Command | What it does |
|---|---|
| `/start` | Welcome, menu and the **Open Job Finder App** button |
| `/jobs` | Browse newest jobs by role, with pages and a detail view |
| `/search <words>` | Search titles, companies, tags and descriptions |
| `/remote` | Remote-only jobs |
| `/profile` | Pick roles, type skills, toggle remote-only. Used for matching |
| `/alerts` | Opt in or out of new-job alerts (only jobs matching your profile, max 5 per check) |
| `/saved` | Your saved jobs |
| `/help` | Help |

**Admin-only** (users listed in `ADMIN_IDS`): `/post <job_id>` or the 📢 button on a job card ·
`/announce` (custom post) · `/refresh` (fetch now) · `/stats`. Every post shows a preview and
needs a **Publish** tap. Admin rights are checked again at publish time.

**Mini App** (dark navy, mobile first): search, role chips, Remote / Salary / Company-board filters,
"For you" matches with a match %, job details with requirements, Apply and Save, a Saved tab,
and a Profile tab that controls alerts.

**Security**
- `BOT_TOKEN` is read only from the environment. It is not in the code, the frontend or the repo,
  and a log filter masks it if a library ever prints it.
- Mini App requests are verified server-side with Telegram's HMAC-SHA256 `initData` check
  (`app/webapp_auth.py`), with a 24 h expiry. The user ID always comes from the verified data, never
  from the browser.
- The webhook only accepts calls carrying the secret `WEBHOOK_SECRET` header Telegram sends.
- All text is HTML-escaped in bot messages, and the Mini App renders with `textContent` only.

---

## 3. Project layout

```
app/
  config.py         settings from environment variables
  db.py             database models (users, jobs, saved_jobs, sent_alerts)
  classify.py       role detection, requirements and salary extraction
  services.py       search, matching, saving, refresh, alerts
  webapp_auth.py    Telegram Mini App initData validation
  main.py           FastAPI app, webhook, Mini App API, scheduler
  outreach/         AI job agent: search.py (providers), llm.py (Gemini/OpenAI), agent.py, api.py,
                    automation.py (full automation scheduler + bot notifications)
  bot/automation_handlers.py   /auto_* commands and lead/automation buttons (admin-only)
  bot/handlers.py   all bot commands and admin tools
  bot/format.py     message and channel-post formatting
  sources/providers.py   job feed connectors
webapp/             Mini App (index.html, styles.css, app.js, agent.js, automation.js, admin.js, profile.js)
.github/workflows/keepalive.yml   pings /health every 10 min so Render's free plan doesn't sleep
tests/              automated tests
render.yaml         Render blueprint (web service + PostgreSQL)
requirements.txt    dependencies
.env.example        list of settings (no real values)
```

---

## 4. Deploy from an Android phone (GitHub + Render)

You need: your bot in Telegram, a free **GitHub** account, a free **Render** account (sign in with GitHub).

### Step A: Secure your bot token
Your old token was shared before, so replace it:
1. Open **@BotFather**, send `/revoke`, pick your bot. The old token stops working.
2. BotFather shows a **new token**. Keep that chat open. You'll paste the token **only into Render** (Step D).
   Never into the code, GitHub, or any chat.

### Step B: Get your Telegram user ID (for admin)
Open **@userinfobot**, press Start, and note the number it shows (e.g. `123456789`).

### Step C: Put the code on GitHub (using Termux)
1. Install **Termux** from F-Droid (the Play Store version is outdated).
2. Download the project zip to your phone (it lands in *Downloads*).
3. In Termux, run these one at a time:
   ```bash
   pkg update -y && pkg install -y git unzip
   termux-setup-storage            # tap Allow
   cd ~ && unzip ~/storage/downloads/web3-job-bot.zip
   cd web3-job-bot
   git init && git add . && git commit -m "Web3 Job Finder bot"
   ```
   (If git asks who you are: `git config --global user.email "you@example.com"` and
   `git config --global user.name "Your Name"`, then run the commit again.)
4. On github.com (Chrome): **+ → New repository**, name it `web3-job-bot`, choose **Private**,
   don't add a README, then **Create**.
5. Make a push key: GitHub → profile photo → **Settings → Developer settings → Personal access tokens →
   Fine-grained → Generate new token**, give it access to this repo with **Contents: Read and write**.
   Copy it.
6. Back in Termux (swap in your username):
   ```bash
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/web3-job-bot.git
   git push -u origin main
   ```
   Username = your GitHub username. Password = paste the access token (it stays invisible while you type).
   The `.gitignore` already keeps `.env` and database files out of GitHub.

### Step D: Deploy on Render
1. On render.com: **New → Blueprint**, connect GitHub, pick `web3-job-bot`.
2. Render reads `render.yaml` and asks for these values:
   - `BOT_TOKEN`: paste the **new** token from BotFather
   - `PUBLIC_BASE_URL`: type `https://web3-job-finder.onrender.com` for now (fix it in step 4 if Render gives a different name)
   - `ADMIN_IDS`: your number from @userinfobot
   - `CHANNEL_ID`: your channel, e.g. `@my_web3_jobs` (or leave empty if you have none yet)
   `WEBHOOK_SECRET` and `DATABASE_URL` are filled in automatically.
3. Tap **Apply**. The first build takes about 3–5 minutes.
4. Open the service and copy its URL at the top. If it differs from what you typed, go to
   **Environment → PUBLIC_BASE_URL**, fix it, and **Save** (Render redeploys).
5. Check `https://YOUR-URL/health`. It should say `"status":"ok"` and, after a minute, a job count.

On startup the app registers the webhook, the command menu and the **Jobs App** menu button by itself.

### Step E: Channel posting (optional)
Open your channel → **Admins → Add admin** → your bot → allow **Post messages**.
Then in the bot: tap a job → **📢 Post to channel** → **Publish**, or use `/announce`.

### Step F: Test it
Send `/start` to your bot → `/profile` (choose roles, add skills) → `/alerts` → turn on → tap **Jobs App**.

---

## 5. Changing the token later
Render → your service → **Environment** → edit `BOT_TOKEN` → **Save changes**. That's the only
place it lives. Never put it in `render.yaml`, the code, or a commit.

---

## 6. Good to know about Render's free plan
- A free web service **sleeps after ~15 minutes** without traffic. A Telegram message wakes it, but the
  first reply can take up to a minute, and hourly job fetching and full automation pause while it sleeps.
  See **Keep the free service awake** at the end of this file. Alerts still go
  out whenever it's awake. The **Starter** plan (about $7/month) keeps it always on.
- Render's **free PostgreSQL expires after 30 days**. Upgrade it, or use a free external Postgres
  (e.g. Neon) by pasting its connection string into `DATABASE_URL`.

---

## 7. Run on a computer (optional)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill BOT_TOKEN, ADMIN_IDS; set BOT_MODE=polling
uvicorn app.main:app --reload
pytest                        # run the tests
```
The Mini App needs a public https URL, so test it on Render (polling mode still runs every bot command).

---

## 8. Safety notice shown to users
Real employers never ask you to pay a fee, buy tokens, connect a wallet, or share a seed phrase to
apply. Third-party (🔎) listings should be confirmed on the company's official website.

## Personal Telegram account login (owner only)

This is a **user client** (Telegram's official MTProto API via Telethon), not the bot. It signs in to
your own account with Telegram's normal login: phone number → login code → optional 2FA password.

**Setup**
1. Get your numeric Telegram ID: send `/myid` to the bot. Put it in `ADMIN_IDS` on Render
   (Environment) and redeploy. Only IDs in `ADMIN_IDS` can open the Account tab.
2. Get API keys: open https://my.telegram.org → log in with your phone → **API development tools** →
   create an app (any title/short name, platform "Web"). Copy **api_id** and **api_hash**.
3. In the Mini App → **🔐 Account** tab → enter api_id and api_hash (stored encrypted), or set
   `TG_API_ID` / `TG_API_HASH` on Render instead.
4. Tap **Login with Telegram Account**, enter your phone in international format, then the code
   Telegram sends to your Telegram app, then your 2FA password if you use one.
5. Tap **Test connection** to confirm the session works live.

**Security**
- Codes and passwords are typed only in the Mini App, sent over HTTPS straight to Telegram, never stored or logged.
- The session is encrypted (Fernet) with `SESSION_ENC_KEY` (falls back to `WEBHOOK_SECRET`). Changing the key forces a new login.
- **Disconnect** logs the session out at Telegram and deletes it. You can also end it in Telegram → Settings → Devices.
- Never share a login code in any chat. Telegram blocks logins whose codes were forwarded in chats.

## AI Job Discovery & Outreach Agent

The owner (IDs in `ADMIN_IDS`) gets a different navigation in the Mini App:
**Home** (dashboard, Find Jobs, Stop Agent) · **Jobs** (company boards + X / Google / Telegram leads with filters) ·
**Outreach** (pending approval, queued, sent, skipped/failed, contacts) · **Tracker** (applications, saved jobs) ·
**Settings** (Telegram account, professional profile, agent limits, job alerts, logs). Public users keep the original app.

**Pipeline:** discover → read the public source page → qualify (role, pay, remote, deadline, apply method,
contacts, freshness, scam flags, relevance) → dedupe (URL + near-duplicate text) → draft → you approve/edit/skip/reject →
send from your Telegram account (Telegram only) or copy/open for X and email → recorded, never messaged twice.

**Safety rails:** nothing is sent without your tap; max messages per 24h (default 10, cap 30); minimum gap between
messages (default 180s); one message per recipient ever; only real people (not channels/bots) can be messaged;
Telegram `FloodWait`/`PeerFlood` automatically stops the agent; high-scam-risk leads can't be drafted; **Stop Agent**
halts searching and sending immediately. AI drafts that add numbers/claims not in your profile or the post are discarded.

| Env var | What for | Where to get it |
|---|---|---|
| `LLM_API_KEY` (**recommended, free**) | One Google Gemini key does AI scoring, message drafts, **Google search** (Gemini "Grounding with Google Search") and **X hiring-post search** | aistudio.google.com/apikey → Create API key. Default base `https://generativelanguage.googleapis.com/v1beta/openai`, model `gemini-2.5-flash` |
| `SERPER_API_KEY` *or* `BRAVE_API_KEY` (optional) | Paid Google-style search, tried before Gemini | serper.dev / brave.com/search/api |
| `X_BEARER_TOKEN` *or* `TWITTERAPI_IO_KEY` (optional) | X post search, tried before Gemini | developer.x.com / twitterapi.io |
| `OPENAI_API_KEY` (optional) | ChatGPT, only used when `LLM_API_KEY` is empty or as a fallback when Gemini fails | platform.openai.com |
| Telegram account | channel reading + sending | Mini App → Settings → Telegram account (see section above) |

**Search order (first that works wins, failures are logged and skipped with back-off, results are never invented):**
- Google: Serper → Brave → Gemini grounding (one grounded call per run for all search phrases).
- X: X API → TwitterAPI.io → Gemini grounding on `site:x.com` → ChatGPT web search.
- Telegram: public channels you list, read through your connected personal account.

**How Gemini grounding stays honest:** the app calls `models/{model}:generateContent` with the `google_search` tool and
keeps a result **only if it is one of the sources Google returned** in `groundingMetadata.groundingChunks`. Those are
`vertexaisearch.cloud.google.com` redirect links; each is resolved to the real page, which is then downloaded and read
(that's what "source checked" means). Anything the model mentions that isn't a grounding source is dropped. For X, only
grounded `x.com/twitter.com/<user>/status/<id>` links are kept, and the real post text is fetched from X's public
oEmbed endpoint when available.

**Free-tier guards (Gemini):** every lead is scored by rules first and only the top `LLM_MAX_CALLS_PER_RUN` (default 5)
new leads per run go to the AI; at most `LLM_DAILY_CALL_CAP` (200) AI calls and `GEMINI_GROUNDING_DAILY_CAP` (150)
grounded searches per UTC day; calls are spaced `LLM_MIN_INTERVAL_SEC` (4 s) apart; HTTP 429 is retried with the
delay Google asks for, then the provider cools down and rules are used. An invalid key (Google answers HTTP 400
"API key not valid") is reported as such and not retried for 30 minutes. These counters live in memory and reset on
restart. If `gemini-2.5-flash` isn't available to a new key, the app retries once with `LLM_FALLBACK_MODEL`
(`gemini-flash-latest`); you can also set `LLM_MODEL` yourself.

Google's own Custom Search JSON API is closed to new customers, so it isn't used. X DMs aren't automated:
the app copies your message and opens the profile so you send it yourself.

## Multi-user, referrals and admin panel

Everyone who opens the Mini App from the bot is signed in by Telegram itself. The server checks
Telegram's signed `initData` (HMAC with the bot token, max age `INITDATA_MAX_AGE`) on every API call
and never trusts a user id or admin flag sent by the browser. No passwords or login codes are asked
from normal users.

- **Every user** gets the job board, the AI job agent (their own leads, drafts, tracker, agent profile)
  and a **Profile** tab: Telegram connection status, personal invite link (Copy / Share), invite stats,
  leaderboard, today's limits and sign-out (clears this device's app data only).
- **Admin-only**: sending from the personal Telegram account, reading Telegram channels, auto-search,
  and the **Admin** tab (dashboard with DAU/WAU/MAU and a 30-day chart, users table with search,
  status filter, pagination, CSV export, suspend/reactivate with an audit log, referral reward settings).
- **Admins** = union of `ADMIN_IDS` and `ADMIN_TELEGRAM_IDS`. Admins can't suspend themselves or other admins.
- **Referrals**: `https://t.me/<bot>?start=ref_<code>` (bot) and, when `MINIAPP_SHORT_NAME` is set,
  `https://t.me/<bot>/<short>?startapp=ref_<code>` (Mini App). Only brand-new users are attributed,
  one inviter per user ever (database unique key), no self-invites, invites from suspended users are
  ignored, bot accounts are rejected, and at most `REFERRAL_HOURLY_CAP` valid invites per inviter per hour.
  Rewards are **points only** (no cash or token value), configured by an admin.
- **Limits**: `USER_DAILY_SEARCHES` search runs and `USER_DAILY_DRAFTS` message drafts per user per day
  (UTC). Admins are unlimited. API calls are rate limited per user and per IP (in memory).
- **Database upgrade**: on startup the app adds the new columns to the existing `users` table with
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (Postgres) and creates the new tables. Nothing is dropped.

## Full automation mode (admins)

Home → **Full automation** card (or the bot commands `/auto_start`, `/auto_pause`, `/auto_stop`, `/auto_status`).

- **States:** Running (searches on schedule) · Paused (kept on, no searches, no reminders) · Stopped (off).
  Saved in the database, so after a restart or redeploy it resumes by itself (a run cut off by the restart is marked
  "interrupted" and the next one starts on time).
- **Scheduler:** a background loop inside the web service checks every 15 s. It does not need the Mini App to be open.
  Opening the Mini App while automation is running starts a search right away, unless one is running or one started
  less than 5 minutes ago.
- **Settings:** search every N minutes (default 15, min 5), status reminder every N minutes (default 1, min 1),
  sources (Google, X, Telegram), minimum match % for notifications (default 50).
- **Each run:** searches the configured providers, dedupes against every lead you already have (URL + near-duplicate
  text), scores and stores new leads, then **the bot** (not your personal account) messages you the new leads with
  match ≥ your setting and scam risk not high: title, project, pay, source link, scam flags and buttons
  **Save / Generate draft / Skip**. More than 5 new leads arrive as one digest. A lead is never announced twice
  (database unique key per admin + lead).
- **Status reminder:** while running, the bot keeps one status message (pinned) up to date every reminder interval by
  editing it (no new message each minute). If that message was deleted, a new one is sent and remembered. Buttons:
  Start / Pause / Stop / Run now.
- **Telegram limits:** ≤ 1 bot message per second per chat, `RetryAfter` honoured. If you never pressed **Start** in
  the bot (or blocked it), Telegram refuses the message: it's shown as a notification error in the card and the leads
  are announced once you start the bot.
- **Safety:** messages to recruiters are **never** sent automatically. "Generate draft" only creates a draft; sending
  still needs your approval in Outreach, with all the existing limits.
- **Logs:** Home → Full automation → Automation logs (runs, provider errors, notification problems).

## Keep the free service awake (Render)

Render's free web service sleeps after about 15 minutes without incoming traffic, and a sleeping service runs no
scheduler. Two options:

1. **GitHub Actions** (included): `.github/workflows/keepalive.yml` calls
   `https://web3-job-finder.onrender.com/health` every 10 minutes. GitHub can delay scheduled runs and turns them off
   after 60 days without repository activity, so treat it as best-effort. If your service URL is different, edit it
   in the workflow file.
2. **External pinger (most reliable):** create a free job at **cron-job.org** (or UptimeRobot) that requests
   `https://web3-job-finder.onrender.com/health` every 10 minutes.

Keeping a free service awake all month uses about 744 instance hours, within Render's 750 free hours for one service.
The paid Starter plan never sleeps.
