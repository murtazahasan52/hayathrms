import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

APP_NAME = os.environ.get("APP_NAME", "Hayat HRMS")
def _pick_mongo_url():
    """MONGO_URL, or the MONGODB_URI / MONGODB_URL that Vercel's MongoDB Atlas integration adds by itself.
    A value that is not a mongodb:// address (e.g. left-over example text) is skipped."""
    for name in ("MONGO_URL", "MONGODB_URI", "MONGODB_URL", "DATABASE_URL"):
        v = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if v.upper().startswith("MONGO_URL="):
            v = v.split("=", 1)[1].strip()
        if v.startswith(("mongodb://", "mongodb+srv://")):
            return v
    return os.environ.get("MONGO_URL") or "mongodb://localhost:27017"


MONGO_URL = _pick_mongo_url()
DB_NAME = os.environ.get("DB_NAME", "hayat_hrms")
# no JWT_SECRET set: derive one from the database address (which holds the DB password), so it is
# unique and private for every deployment instead of a fixed known text
JWT_SECRET = os.environ.get("JWT_SECRET") or ("hayat-" + __import__("hashlib").sha256(
    ("hayat-hrms-jwt|" + MONGO_URL).encode()).hexdigest())
JWT_HOURS = int(os.environ.get("JWT_HOURS", "72"))
SUPERADMIN_EMAIL = os.environ.get("SUPERADMIN_EMAIL", "murtaza@hayatcctv.com")
SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "Admin@123")
# Public URL of this deployment (used for payslip links sent on WhatsApp), e.g. https://hrms.hayatcctv.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Kolkata")
USE_MOCK_DB = os.environ.get("USE_MOCK_DB") == "1"
DISABLE_SCHEDULER = os.environ.get("DISABLE_SCHEDULER") == "1"
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")
