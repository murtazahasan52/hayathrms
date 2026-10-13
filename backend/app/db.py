from .config import MONGO_URL, DB_NAME, USE_MOCK_DB

if USE_MOCK_DB:
    from mongomock_motor import AsyncMongoMockClient
    client = AsyncMongoMockClient()
else:
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=6000)  # fail fast with a clear message if MongoDB is down

db = client[DB_NAME]


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
