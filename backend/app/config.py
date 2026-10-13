import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

APP_NAME = os.environ.get("APP_NAME", "Hayat HRMS")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "hayat_hrms")
JWT_SECRET = os.environ.get("JWT_SECRET", "change-this-secret-in-production")
JWT_HOURS = int(os.environ.get("JWT_HOURS", "72"))
SUPERADMIN_EMAIL = os.environ.get("SUPERADMIN_EMAIL", "murtaza@hayatcctv.com")
SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "Admin@123")
# Public URL of this deployment (used for payslip links sent on WhatsApp), e.g. https://hrms.hayatcctv.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Kolkata")
USE_MOCK_DB = os.environ.get("USE_MOCK_DB") == "1"
DISABLE_SCHEDULER = os.environ.get("DISABLE_SCHEDULER") == "1"
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")
