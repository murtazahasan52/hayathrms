"""Helpers for 'Sync all', auto-adding machine users, and deleting punches without them coming back on re-sync."""
import datetime
from pymongo import InsertOne
from ..db import db
from ..defaults import company_settings
from ..utils import new_id, utcnow_iso
from .attendance import process_range


async def linked_codes(cid):
    codes = set()
    async for e in db.employees.find({"company_id": cid}, {"code": 1, "extra_codes": 1}):
        codes.add(e.get("code"))
        codes.update(e.get("extra_codes") or [])
    return codes


async def auto_add_employees(company, force=False):
    """Create employees for machine user IDs not linked to anyone (names taken from the machine)."""
    if not force and not company_settings(company).get("auto_add_employees"):
        return 0
    cid = company["id"]
    linked = await linked_codes(cid)
    ignored = set(company.get("ignored_codes") or [])
    limit = int(company.get("max_employees") or 10 ** 6)
    active = await db.employees.count_documents({"company_id": cid, "active": {"$ne": False}})
    added = 0
    new_ids = []
    async for u in db.device_users.find({"company_id": cid}, {"_id": 0}):
        code = u["user_code"]
        if code in linked or code in ignored:
            continue
        if active + added >= limit:
            break
        eid = new_id()
        new_ids.append(eid)
        await db.employees.insert_one({
            "id": eid, "company_id": cid, "code": code, "name": u.get("name") or f"Employee {code}",
            "salary_type": "monthly", "earnings": [{"name": "Basic", "amount": 0}], "ot_allowed": True,
            "active": True, "auto_added": True, "created_at": utcnow_iso(),
            **({"photo_v": u["photo_v"]} if u.get("photo_v") else {})})
        linked.add(code)
        added += 1
    if added:
        await db.device_users.delete_many({"company_id": cid, "user_code": {"$in": list(linked)}})
        today = datetime.date.today()
        await process_range(company, today - datetime.timedelta(days=62), today, new_ids)
    return added


async def purge_punches(company, query):
    """Delete punches matching query, remember them so a re-sync from the machine does not bring them back,
    then recalculate attendance for the affected days."""
    cid = company["id"]
    query = dict(query, company_id=cid)
    rows = await db.punches.find(query, {"_id": 0, "emp_code": 1, "ts": 1, "date": 1, "source": 1}).to_list(200000)
    if not rows:
        return 0
    ops = [InsertOne({"company_id": cid, "emp_code": r["emp_code"], "ts": r["ts"], "date": r["date"],
                      "deleted_at": utcnow_iso()}) for r in rows if r.get("source") != "manual"]
    for i in range(0, len(ops), 1000):
        await db.deleted_punches.bulk_write(ops[i:i + 1000], ordered=False)
    await db.punches.delete_many(query)
    dates = sorted({r["date"] for r in rows})
    codes = {r["emp_code"] for r in rows}
    emp_ids = [e["id"] async for e in db.employees.find(
        {"company_id": cid, "$or": [{"code": {"$in": list(codes)}}, {"extra_codes": {"$in": list(codes)}}]}, {"id": 1})]
    if emp_ids:
        d1 = datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=1)
        d2 = datetime.date.fromisoformat(dates[-1])
        await process_range(company, d1, d2, emp_ids)
    return len(rows)
