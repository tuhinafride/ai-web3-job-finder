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
})
