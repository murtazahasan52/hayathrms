"""Shift changes with an effective date, bulk shift change, and the day-wise roster."""
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from pymongo import UpdateOne
from ..db import db, bulk_upsert
from ..auth import company_ctx, Ctx
from ..utils import new_id, utcnow_iso, daterange
from ..services.attendance import process_range, get_shifts, shift_for

router = APIRouter(prefix="/api")


def _today():
    return datetime.date.today()


async def apply_shift_change(ctx, emp, shift_id, from_date, note=""):
    """Record that `emp` works `shift_id` from `from_date`. Earlier days keep the shift they had."""
    hist = sorted(emp.get("shift_history") or [], key=lambda h: h.get("from") or "")
    if not hist:
        hist = [{"from": "1900-01-01", "shift_id": emp.get("shift_id"), "note": "original"}]
    hist = [h for h in hist if h.get("from") != from_date]
    hist.append({"from": from_date, "shift_id": shift_id, "note": note, "by": ctx.user.get("name"), "at": utcnow_iso()})
    hist.sort(key=lambda h: h["from"])
    current = hist[0]["shift_id"]
    for h in hist:
        if h["from"] <= _today().isoformat():
            current = h["shift_id"]
    await db.employees.update_one({"id": emp["id"]}, {"$set": {"shift_history": hist, "shift_id": current}})


@router.post("/shifts/change")
async def change_shift(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Bulk shift change: {employee_ids, shift_id, from_date, note}. Recalculates attendance from that date."""
    ids = data.get("employee_ids") or []
    sid = data.get("shift_id")
    frm = data.get("from_date") or _today().isoformat()
    shifts, _ = await get_shifts(ctx.cid)
    if not ids:
        raise HTTPException(400, "Select employees first")
    if sid not in shifts:
        raise HTTPException(400, "Choose a shift")
    emps = await db.employees.find({"company_id": ctx.cid, "id": {"$in": ids}}, {"_id": 0}).to_list(10000)
    for e in emps:
        await apply_shift_change(ctx, e, sid, frm, data.get("note", ""))
    d1 = datetime.date.fromisoformat(frm)
    if d1 <= _today():
        await process_range(ctx.company, d1, _today(), [e["id"] for e in emps])
    return {"ok": True, "changed": len(emps)}


@router.get("/shifts/history/{eid}")
async def shift_history(eid: str, ctx: Ctx = Depends(company_ctx)):
    e = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0, "shift_history": 1, "shift_id": 1})
    if not e:
        raise HTTPException(404, "Employee not found")
    return sorted(e.get("shift_history") or [], key=lambda h: h.get("from") or "", reverse=True)


@router.post("/shifts/history/{eid}/remove")
async def remove_history(eid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    e = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    if not e:
        raise HTTPException(404, "Employee not found")
    frm = data.get("from")
    hist = [h for h in (e.get("shift_history") or []) if h.get("from") != frm]
    if hist and hist[0].get("from") != "1900-01-01":
        hist.sort(key=lambda h: h["from"])
    current = hist[0]["shift_id"] if hist else e.get("shift_id")
    for h in sorted(hist, key=lambda h: h["from"]):
        if h["from"] <= _today().isoformat():
            current = h["shift_id"]
    await db.employees.update_one({"id": eid}, {"$set": {"shift_history": hist, "shift_id": current}})
    if frm and frm <= _today().isoformat():
        await process_range(ctx.company, datetime.date.fromisoformat(max(frm, "2000-01-01")), _today(), [eid])
    return {"ok": True}


# ---------------- roster ----------------
@router.get("/roster")
async def get_roster(from_date: str, to_date: str, ctx: Ctx = Depends(company_ctx)):
    d1, d2 = datetime.date.fromisoformat(from_date), datetime.date.fromisoformat(to_date)
    if (d2 - d1).days > 41:
        raise HTTPException(400, "Max 6 weeks at a time")
    shifts, default = await get_shifts(ctx.cid)
    emps = await db.employees.find({"company_id": ctx.cid, "active": {"$ne": False}},
                                   {"_id": 0, "id": 1, "code": 1, "name": 1, "department": 1, "shift_id": 1,
                                    "shift_history": 1, "weekly_off": 1, "photo_v": 1}).to_list(10000)
    emps.sort(key=lambda e: (len(e.get("code", "")), e.get("code", "")))
    roster = {}
    async for r in db.roster.find({"company_id": ctx.cid, "date": {"$gte": from_date, "$lte": to_date}}, {"_id": 0}):
        roster[(r["employee_id"], r["date"])] = r["shift_id"]
    days = [d.isoformat() for d in daterange(d1, d2)]
    rows = []
    for e in emps:
        cells = []
        for ds in days:
            sh, rv = shift_for(e, ds, shifts, default, roster)
            wo = e.get("weekly_off") if e.get("weekly_off") is not None else sh.get("weekly_off", [6])
            off = rv == "OFF" or (not rv and datetime.date.fromisoformat(ds).weekday() in (wo or []))
            cells.append({"shift_id": sh.get("id"), "off": off, "set": rv})
        rows.append({"employee": {k: e.get(k) for k in ("id", "code", "name", "department", "photo_v")}, "cells": cells})
    return {"days": days, "rows": rows, "shifts": list(shifts.values())}


async def _write_roster(ctx, entries):
    ops, touched = [], {}
    for x in entries:
        eid, ds, sid = x["employee_id"], x["date"], x.get("shift_id")
        datetime.date.fromisoformat(ds)
        if sid:
            ops.append(UpdateOne({"company_id": ctx.cid, "employee_id": eid, "date": ds},
                                 {"$set": {"shift_id": sid, "updated_at": utcnow_iso(), "by": ctx.user.get("name")}},
                                 upsert=True))
        else:
            await db.roster.delete_many({"company_id": ctx.cid, "employee_id": eid, "date": ds})
        touched.setdefault(eid, []).append(ds)
    for i in range(0, len(ops), 1000):
        await bulk_upsert(db.roster, ops[i:i + 1000])
    today = _today().isoformat()
    past = [ds for v in touched.values() for ds in v if ds <= today]
    if past:
        await process_range(ctx.company, datetime.date.fromisoformat(min(past)), _today(), list(touched.keys()))
    return len(entries)


@router.put("/roster")
async def put_roster(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """{cells: [{employee_id, date, shift_id}]} — shift_id = a shift id, 'OFF', or null to clear."""
    shifts, _ = await get_shifts(ctx.cid)
    cells = data.get("cells") or []
    for c in cells:
        if c.get("shift_id") not in (None, "", "OFF") and c["shift_id"] not in shifts:
            raise HTTPException(400, "Unknown shift")
    n = await _write_roster(ctx, cells)
    return {"ok": True, "saved": n}


@router.post("/roster/pattern")
async def roster_pattern(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Fill the roster for many days.
    mode 'weekly': week = [7 values Mon..Sun] (shift id / 'OFF' / '' = no change)
    mode 'rotate': rotation = [shift ids], every = N days per shift (e.g. day/night swap each week), off_days = [weekday ints]"""
    ids = data.get("employee_ids") or []
    d1 = datetime.date.fromisoformat(data["from_date"])
    d2 = datetime.date.fromisoformat(data["to_date"])
    if not ids or d2 < d1 or (d2 - d1).days > 366:
        raise HTTPException(400, "Choose employees and a date range up to one year")
    entries = []
    if data.get("mode") == "rotate":
        rot = [x for x in data.get("rotation") or [] if x]
        every = max(1, int(data.get("every") or 7))
        offs = set(int(x) for x in data.get("off_days") or [])
        if not rot:
            raise HTTPException(400, "Choose the shifts to rotate")
        for i, d in enumerate(daterange(d1, d2)):
            val = "OFF" if d.weekday() in offs else rot[(i // every) % len(rot)]
            entries += [{"employee_id": e, "date": d.isoformat(), "shift_id": val} for e in ids]
    else:
        week = data.get("week") or []
        if len(week) != 7:
            raise HTTPException(400, "Weekly pattern needs 7 values")
        for d in daterange(d1, d2):
            v = week[d.weekday()]
            if v == "CLEAR":
                entries += [{"employee_id": e, "date": d.isoformat(), "shift_id": None} for e in ids]
            elif v:
                entries += [{"employee_id": e, "date": d.isoformat(), "shift_id": v} for e in ids]
    n = await _write_roster(ctx, entries)
    return {"ok": True, "saved": n}
