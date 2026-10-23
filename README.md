# AI Web3 Job Finder — Telegram Mini App MVP

A starter project for a Telegram bot + Telegram Mini App that collects public Web3 job posts, lets users search/filter them, and sends opt-in Telegram alerts.

## Included
- Telegram bot: `/start`, `/jobs`, `/alerts_on`, `/alerts_off`, `/help`
- Mini App UI: search, role filter, salary filter, refresh, open job source
- RSS-based job collection from configurable feeds
- Basic duplicate detection and scheduled new-job alerts
- Demo fallback jobs if feeds are unavailable

## Important limits
- This is a starter project, not a deployed live service.
- To work inside Telegram, you need a Telegram bot token from `@BotFather`, a public HTTPS URL for the Mini App, and a server that stays online.
- You cannot use a Telegram bot to cold-DM arbitrary founders or recruiters. Alerts go only to users who start/subscribe to your bot.
- Job listings are not guaranteed legitimate. Verify each company, recruiter, domain, and application link before sharing personal data.
- The “AI” layer here is rule-based role matching and filtering. You can add an LLM API later, but many APIs are paid.

## 1. Configure
Copy `.env.example` to `.env` and set:
- `BOT_TOKEN`: token from `@BotFather`
- `WEBAPP_URL`: public HTTPS base URL of this running app (same service serves the Mini App and API)
- `PORT`: defaults to 8080

## 2. Install and run locally
Python 3.11+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

The bot polling and API run together. For local testing, expose the webapp folder with HTTPS using a host or tunnel, then set `WEBAPP_URL` to that HTTPS URL and restart.

## 3. Create the Telegram bot
1. Open Telegram and message `@BotFather`.
2. Run `/newbot`, choose a name and username, then copy the token.
3. Put the token in `.env`.
4. Host `webapp/` on an HTTPS static host (for example, a free static-hosting tier) and set that URL as `WEBAPP_URL`.
5. Deploy the Python service on a host that supports a persistent process. Free tiers may sleep or change limits.
6. In BotFather, use `/setmenubutton` and set the Mini App URL, or use the bot's Open App button.

## 4. Add job sources
Edit `RSS_FEEDS` in `app.py`. Use public job RSS feeds you are allowed to access. Avoid scraping sites that prohibit it. Some feeds can change or stop working.

## 5. Before public launch
- Replace the demo fallback listings with verified sources or label them as demo data.
- Add moderation, source validation, rate limiting, privacy policy, and error monitoring.
- Never promise a salary or job is real unless verified.


## Easiest deployment path (phone-friendly)
1. Create a private GitHub repository and upload the contents of this ZIP.
2. Create a web service on Render and connect that repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python app.py`
5. Add environment variables in Render:
   - `BOT_TOKEN` = your BotFather token (keep it secret)
   - `WEBAPP_URL` = your deployed service URL, e.g. `https://YOUR-SERVICE.onrender.com`
   - `ALERT_INTERVAL_SECONDS` = `900`
6. Deploy. After the service is live, open `https://YOUR-SERVICE.onrender.com/health`.
7. In BotFather, set the Mini App menu button to the same HTTPS service URL.

Note: Free hosting availability and sleep limits can change. If the service sleeps, bot polling and scheduled alerts may pause. For a reliable always-on bot, a paid always-on service may be needed.
