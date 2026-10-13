"""One-time setup (indexes + first super admin). Runs at startup, and again lazily on first login —
serverless hosts like Vercel do not always run the startup event."""
from .config import SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD
from .db import db, ensure_indexes
from .auth import hash_pw
from .utils import new_id, utcnow_iso

_done = False


async def ensure_ready():
    global _done
    if _done:
        return
    try:
        await ensure_indexes()
    except Exception as e:  # never block login because of an index
        print("index warning:", e)
    if not await db.users.find_one({"role": "superadmin"}):
        await db.users.insert_one({"id": new_id(), "email": SUPERADMIN_EMAIL.lower(), "name": "Super Admin",
                                   "role": "superadmin", "active": True, "password_hash": hash_pw(SUPERADMIN_PASSWORD),
                                   "created_at": utcnow_iso()})
        print(f"Super admin created: {SUPERADMIN_EMAIL} / {SUPERADMIN_PASSWORD}  (change the password after login)")
    _done = True
