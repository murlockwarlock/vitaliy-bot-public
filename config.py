import os
import sys
from dotenv import load_dotenv

IS_TESTING = (
    os.getenv("TESTING", "").lower() in ("1", "true", "yes")
    or "pytest" in sys.modules
    or any("test" in arg for arg in sys.argv)
)

if not IS_TESTING:
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    if IS_TESTING:
        BOT_TOKEN = "000000000:TEST_BOT_TOKEN"
    else:
        raise ValueError("BOT_TOKEN env variable is missing")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///database.db")
LANDING_WEBHOOK_HOST = os.getenv("LANDING_WEBHOOK_HOST", "127.0.0.1")
LANDING_WEBHOOK_PORT = int(os.getenv("LANDING_WEBHOOK_PORT", "8081"))
LANDING_WEBHOOK_SECRET = os.getenv("LANDING_WEBHOOK_SECRET", "")

owner_ids_str = os.getenv("OWNER_IDS", "")
if IS_TESTING and not owner_ids_str:
    OWNER_IDS = [999999999]
else:
    OWNER_IDS = [int(i.strip()) for i in owner_ids_str.split(",") if i.strip()]
