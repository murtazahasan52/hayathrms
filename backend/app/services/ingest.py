"""Single entry point for punches coming from any source (ADMS push, Hikvision push, agent, direct pull, manual)."""
import os
import asyncio
import datetime
from pymongo import UpdateOne
from ..db import db, bulk_upsert
from ..defaults import whatsapp_settings
from ..utils import tz_of, now_local, utcnow_iso, new_id
from .attendance import process_range
from . import whatsapp

IN_STATES = {0, 3, 4}
OUT_STATES = {1, 2, 5}
HIK_DIR = {"checkin": "in", "checkout": "out", "breakin": "in", "breakout": "out",
           "overtimein": "in", "overtimeout": "out"}


def norm_ts(ts, tz):
    if isinstance(ts, str):
        s = ts.strip().replace("T", " ").replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
    else:
        dt = ts
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def direction_of(row):
    if row.get("direction") in ("in", "out"):
        return row["direction"]
    st = row.get("state")
    if st is not None and str(st).strip().lstrip("-").isdigit():
        st = int(st)
        if st in IN_STATES:
            return "in"
        if st in OUT_STATES:
            return "out"
    a = str(row.get("attendance_status") or "").lower()
    return HIK_DIR.get(a)


async def ingest(company, device, rows, source):
    """rows: [{user_id, ts, state?, verify?, attendance_status?, direction?, name?}] -> number of NEW punches."""
    if not rows:
        if device:
            await db.devices.update_one({"id": device["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online"}})
        return 0
    tz = tz_of(company)
    cid = company["id"]
    docs = []
    for r in rows:
        try:
            dt = norm_ts(r["ts"], tz)
        except Exception:
            continue
        code = str(r.get("user_id") or "").strip()
        if not code:
            continue
        docs.append({"id": new_id(), "company_id": cid, "device_id": device["id"] if device else None,
                     "device_name": device.get("name") if device else "Manual", "emp_code": code,
                     "ts": dt.strftime("%Y-%m-%d %H:%M:%S"), "date": dt.date().isoformat(),
                     "direction": direction_of(r), "verify": str(r.get("verify") or ""), "source": source,
                     "created_at": utcnow_iso()})
    if not docs:
        return 0
    keys = {(d["emp_code"], d["ts"]) for d in docs}
    if source == "manual":
        # a punch added by hand overrides an earlier deletion of the same punch
        for code, ts in keys:
            await db.deleted_punches.delete_many({"company_id": cid, "emp_code": code, "ts": ts})
    else:
        ignored = set(company.get("ignored_codes") or [])
        dates = sorted(d["date"] for d in docs)
        dead = set()
        async for t in db.deleted_punches.find({"company_id": cid, "date": {"$gte": dates[0], "$lte": dates[-1]}},
                                               {"_id": 0, "emp_code": 1, "ts": 1}):
            dead.add((t["emp_code"], t["ts"]))
        docs = [d for d in docs if (d["emp_code"], d["ts"]) not in dead and d["emp_code"] not in ignored]
        if not docs:
            if device:
                await db.devices.update_one({"id": device["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online"}})
            return 0
    ops = [UpdateOne({"company_id": cid, "emp_code": d["emp_code"], "ts": d["ts"]}, {"$setOnInsert": d}, upsert=True)
           for d in docs]
    new_docs = []
    for i in range(0, len(ops), 1000):
        res = await bulk_upsert(db.punches, ops[i:i + 1000])
        for idx in (res.upserted_ids or {}).keys():
            new_docs.append(docs[i + idx])

    if device:
        upd = {"$set": {"last_seen": utcnow_iso(), "status": "online"}}
        if new_docs:
            upd["$set"]["last_punch_at"] = max(d["ts"] for d in new_docs)
            upd["$inc"] = {"total_punches": len(new_docs)}
        await db.devices.update_one({"id": device["id"]}, upd)
    if not new_docs:
        return 0

    codes = sorted({d["emp_code"] for d in new_docs})
    emps = await db.employees.find({"company_id": cid, "$or": [{"code": {"$in": codes}}, {"extra_codes": {"$in": codes}}]},
                                   {"_id": 0}).to_list(5000)
    code_map = {}
    for e in emps:
        for c in [e.get("code")] + list(e.get("extra_codes") or []):
            code_map[str(c)] = e
    unknown = [c for c in codes if c not in code_map]
    for c in unknown:
        nm = next((r.get("name") for r in rows if str(r.get("user_id")) == c and r.get("name")), None)
        await db.device_users.update_one({"company_id": cid, "user_code": c},
                                         {"$set": {"last_punch": max(d["ts"] for d in new_docs if d["emp_code"] == c),
                                                   **({"name": nm} if nm else {})},
                                          "$setOnInsert": {"id": new_id(), "device_id": device["id"] if device else None}},
                                         upsert=True)
    if unknown:
        from .sync import auto_add_employees
        await auto_add_employees(company)
    if emps:
        dates = [datetime.date.fromisoformat(d["date"]) for d in new_docs]
        await process_range(company, min(dates) - datetime.timedelta(days=1), max(dates), [e["id"] for e in emps])
        if os.environ.get("VERCEL"):
            await _realtime_alerts(company, new_docs, code_map)   # no background tasks on serverless
        else:
            asyncio.create_task(_realtime_alerts(company, new_docs, code_map))
    return len(new_docs)


async def _realtime_alerts(company, new_docs, code_map):
    try:
        w = whatsapp_settings(company)
        if not w.get("enabled"):
            return
        ev = w["events"]
        if not (ev.get("punch_alert", {}).get("enabled") or ev.get("late_alert", {}).get("enabled")):
            return
        now = now_local(company)
        window = datetime.timedelta(minutes=int(w.get("realtime_window_minutes") or 15))
        for d in sorted(new_docs, key=lambda x: x["ts"]):
            e = code_map.get(d["emp_code"])
            if not e or not e.get("phone") or e.get("whatsapp_alerts") is False:
                continue
            t = datetime.datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
            if now - t > window or t - now > window:
                continue
            att = await db.attendance.find_one({"company_id": company["id"], "employee_id": e["id"],
                                                "date": d["date"]}, {"_id": 0}) or {}
            first = att.get("in_time") == t.strftime("%H:%M")
            values = {"name": e["name"], "direction": "IN" if first else "OUT", "time": t.strftime("%I:%M %p"),
                      "date": t.strftime("%d-%m-%Y"), "company": company["name"],
                      "late_minutes": att.get("late_minutes", 0)}
            await whatsapp.send_event(company, "punch_alert", e["phone"], values, employee_id=e["id"])
            if first and att.get("late_minutes"):
                await whatsapp.send_event(company, "late_alert", e["phone"], values, employee_id=e["id"])
    except Exception as ex:  # never let alerts break ingestion
        print("realtime alert error:", ex)
