from .config import MONGO_URL, DB_NAME, USE_MOCK_DB

from urllib.parse import quote, unquote

DB_ERROR = ""


def clean_mongo_url(url):
    """Fix the usual copy-paste mistakes in MONGO_URL: spaces/quotes, a 'MONGO_URL=' prefix, and a password with
    @ : / # % etc. that was not URL-escaped (Atlas passwords often have these)."""
    url = (url or "").strip().strip('"').strip("'").strip()
    if url.upper().startswith("MONGO_URL="):
        url = url.split("=", 1)[1].strip()
    for scheme in ("mongodb+srv://", "mongodb://"):
        if url.startswith(scheme):
            rest = url[len(scheme):]
            main, sep, query = rest.partition("?")
            at = main.rfind("@")
            if at > 0:
                creds, hosts = main[:at], main[at + 1:]
                user, colon, pw = creds.partition(":")
                creds = quote(unquote(user), safe="") + (":" + quote(unquote(pw), safe="") if colon else "")
                url = scheme + creds + "@" + hosts + sep + query
            return url
    return url


class _NoDatabase:
    """Stands in for the database when MONGO_URL is wrong, so the site still starts and says what to fix."""
    def __init__(self, msg):
        self._msg = msg

    def __getattr__(self, name):
        raise RuntimeError(self._msg)

    def __getitem__(self, name):
        raise RuntimeError(self._msg)


if USE_MOCK_DB:
    from mongomock_motor import AsyncMongoMockClient
    client = AsyncMongoMockClient()
    db = client[DB_NAME]
else:
    from motor.motor_asyncio import AsyncIOMotorClient
    _url = clean_mongo_url(MONGO_URL)
    if "<db_password>" in MONGO_URL or "<password>" in MONGO_URL:
        DB_ERROR = "MONGO_URL still contains <db_password> — put your real Atlas database password in its place."
    try:
        client = AsyncIOMotorClient(_url, serverSelectionTimeoutMS=6000)  # fail fast with a clear message if MongoDB is down
        db = client[DB_NAME]
    except Exception as e:  # bad MONGO_URL must not crash the whole site
        client = None
        DB_ERROR = DB_ERROR or f"MONGO_URL setting is not valid ({e.__class__.__name__}: {e}). Copy it again from MongoDB Atlas → Connect → Drivers."
        db = _NoDatabase("Database not configured: " + DB_ERROR)


async def ensure_indexes():
    await db.users.create_index("email", unique=True)
    await db.employees.create_index([("company_id", 1), ("code", 1)])
    await db.punches.create_index([("company_id", 1), ("emp_code", 1), ("ts", 1)], unique=True)
    await db.punches.create_index([("company_id", 1), ("date", 1)])
    await db.attendance.create_index([("company_id", 1), ("employee_id", 1), ("date", 1)], unique=True)
    await db.attendance.create_index([("company_id", 1), ("date", 1)])
    await db.payslips.create_index([("company_id", 1), ("month", 1), ("employee_id", 1)], unique=True)
    await db.devices.create_index("serial")
    await db.roster.create_index([("company_id", 1), ("employee_id", 1), ("date", 1)], unique=True)
    await db.photos.create_index([("company_id", 1), ("code", 1)], unique=True)
    await db.devices.create_index("push_token")


class _Res:
    def __init__(self, upserted):
        self.upserted_ids = upserted


async def bulk_upsert(coll, ops):
    """bulk_write(ordered=False); falls back to one-by-one under the in-memory test DB."""
    if not USE_MOCK_DB:
        return await coll.bulk_write(ops, ordered=False)
    up = {}
    for i, op in enumerate(ops):
        r = await coll.update_one(op._filter, op._doc, upsert=True)
        if r.upserted_id is not None:
            up[i] = r.upserted_id
    return _Res(up)
