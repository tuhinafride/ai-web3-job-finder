import os
import tempfile

# Configure a throwaway environment BEFORE the app is imported.
_tmp = tempfile.mkdtemp()
os.environ.update({
    "BOT_TOKEN": "123456:TEST-token-not-real",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
    "DISABLE_SCHEDULER": "true",
    "BOT_MODE": "none",
    "ADMIN_IDS": "999",
    "CHANNEL_ID": "@test_channel",
    "PUBLIC_BASE_URL": "",
    "ADMIN_TELEGRAM_IDS": "1000",
    "BOT_USERNAME": "Test_jobs_bot",
    "RATE_LIMIT_IP_PER_MIN": "1000000",
    "RATE_LIMIT_USER_PER_MIN": "100000",
    "LLM_MIN_INTERVAL_SEC": "0",
})
