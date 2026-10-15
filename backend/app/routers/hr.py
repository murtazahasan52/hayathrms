"""Employees, shifts, holidays, leaves, advances."""
import csv
import io
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, UploadFile, File
from ..db import db
from ..auth import company_ctx, owner_ctx, Ctx
from ..utils import new_id, utcnow_iso, clean_phone
from ..services.attendance import process_range
from ..services.sync import purge_punches
from ..services.photos import save_photo, decode_any, photo_key, photo_url
from fastapi.responses import Response

router = APIRouter(prefix="/api")

EMP_FIELDS = ("code", "extra_codes", "name", "phone", "email", "department", "designation", "shift_id", "joining_date",
              "leaving_date", "salary_type", "earnings", "daily_rate", "hourly_rate", "ot_allowed", "ot_rate_per_hour",
              "pf_enabled", "esi_enabled", "pt_amount", "other_deductions", "bank_account", "ifsc", "uan", "weekly_off",
              "whatsapp_alerts", "active", "gender", "address", "esi_no", "pan", "ot_mode", "ot_fixed_amount")


def _clean_emp(data):
    e = {k: data[k] for k in EMP_FIELDS if k in data}
    if "code" in e:
        e["code"] = str(e["code"]).strip()
    if "phone" in e:
        e["phone"] = clean_phone(e["phone"])
    if "extra_codes" in e:
        v = e["extra_codes"]
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        e["extra_codes"] = [str(x) for x in v if str(x).strip()]
    for k in ("daily_rate", "hourly_rate", "ot_rate_per_hour", "pt_amount", "ot_fixed_amount"):
        if k in e:
            e[k] = float(e[k] or 0)
    for k in ("earnings", "other_deductions"):
        if k in e:
            e[k] = [{"name": x.get("name", ""), "amount": float(x.get("amount") or 0)} for x in (e[k] or []) if x.get("name")]
    for k in ("joining_date", "leaving_date"):
        if k in e and not e[k]:
            e[k] = None
    return e


async def _check_code(cid, code, exclude_id=None):
    q = {"company_id": cid, "$or": [{"code": code}, {"extra_codes": code}]}
    if exclude_id:
        q["id"] = {"$ne": exclude_id}
    if await db.employees.find_one(q):
        raise HTTPException(400, f"Device ID {code} is already given to another employee")


# ---------------- employees ----------------
@router.get("/employees")
async def list_employees(ctx: Ctx = Depends(company_ctx), active: str = "1"):
    q = {"company_id": ctx.cid}
    if active == "1":
        q["active"] = {"$ne": False}
    elif active == "0":
        q["active"] = False
    rows = await db.employees.find(q, {"_id": 0}).to_list(10000)
    rows.sort(key=lambda e: (len(e.get("code", "")), e.get("code", "")))
    key = await photo_key(ctx.company)
    for e in rows:
        e["photo_url"] = photo_url(key, e.get("code"), e.get("photo_v"))
    return rows


@router.put("/employees/bulk")
async def bulk_update(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Quick-edit sheet: {rows: [{id, name, phone, department, designation, shift_id, salary_type, monthly, daily_rate,
    hourly_rate, joining_date}]}. 'monthly' = total monthly salary; existing allowance split is kept in proportion."""
    from .shifts_roster import apply_shift_change
    saved, errors = 0, []
    for row in data.get("rows") or []:
        cur = await db.employees.find_one({"id": row.get("id"), "company_id": ctx.cid}, {"_id": 0})
        if not cur:
            continue
        e = _clean_emp({k: v for k, v in row.items() if k in EMP_FIELDS and k not in ("code", "earnings")})
        if "name" in e and not str(e["name"]).strip():
            errors.append(f"{cur['code']}: name cannot be empty")
            continue
        if "monthly" in row and row["monthly"] not in (None, ""):
            total = float(row["monthly"] or 0)
            lines = [x for x in (cur.get("earnings") or []) if float(x.get("amount") or 0)]
            old = sum(float(x["amount"]) for x in lines)
            if len(lines) > 1 and old:
                e["earnings"] = [{"name": x["name"], "amount": round(float(x["amount"]) * total / old)} for x in lines]
                diff = total - sum(x["amount"] for x in e["earnings"])
                e["earnings"][0]["amount"] += diff
            else:
                e["earnings"] = [{"name": (lines[0]["name"] if lines else "Basic"), "amount": total}]
        new_shift = e.pop("shift_id", "__same__")
        if e:
            await db.employees.update_one({"id": cur["id"]}, {"$set": e})
        if new_shift != "__same__" and (new_shift or None) != (cur.get("shift_id") or None):
            await apply_shift_change(ctx, cur, new_shift or None, datetime.date.today().isoformat(), "quick edit")
        saved += 1
    return {"ok": True, "saved": saved, "errors": errors}


@router.post("/employees/{eid}/photo")
async def upload_photo(eid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    e = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    if not e:
        raise HTTPException(404, "Employee not found")
    try:
        raw = decode_any(data.get("data") or "")
    except Exception:
        raise HTTPException(400, "Could not read the image")
    if len(raw) > 8_000_000:
        raise HTTPException(400, "Image too large (max 8 MB)")
    if not await save_photo(ctx.company, e["code"], raw, "upload"):
        raise HTTPException(400, "Could not read the image")
    e = await db.employees.find_one({"id": eid}, {"_id": 0})
    return {"ok": True, "photo_url": photo_url(await photo_key(ctx.company), e["code"], e.get("photo_v"))}


@router.delete("/employees/{eid}/photo")
async def delete_photo(eid: str, ctx: Ctx = Depends(company_ctx)):
    e = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    if e:
        await db.photos.delete_many({"company_id": ctx.cid, "code": e["code"]})
        await db.employees.update_one({"id": eid}, {"$unset": {"photo_v": ""}})
    return {"ok": True}


@router.get("/public/photo/{key}/{code}.jpg")
async def public_photo(key: str, code: str):
    c = await db.companies.find_one({"photo_key": key}, {"id": 1})
    p = c and await db.photos.find_one({"company_id": c["id"], "code": code}, {"data": 1})
    if not p:
        raise HTTPException(404, "No photo")
    import base64
    return Response(base64.b64decode(p["data"]), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


@router.post("/employees")
async def add_employee(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    e = _clean_emp(data)
    if not e.get("code") or not e.get("name"):
        raise HTTPException(400, "Device user ID and name are required")
    n = await db.employees.count_documents({"company_id": ctx.cid, "active": {"$ne": False}})
    if n >= int(ctx.company.get("max_employees") or 10**6):
        raise HTTPException(403, f"Employee limit ({ctx.company['max_employees']}) reached for your plan")
    await _check_code(ctx.cid, e["code"])
    e.setdefault("salary_type", "monthly")
    e.setdefault("earnings", [{"name": "Basic", "amount": 0}])
    e.setdefault("ot_allowed", True)
    e.setdefault("active", True)
    e.update(id=new_id(), company_id=ctx.cid, created_at=utcnow_iso())
    await db.employees.insert_one(dict(e))
    await db.device_users.delete_many({"company_id": ctx.cid, "user_code": {"$in": [e["code"]] + e.get("extra_codes", [])}})
    await _reprocess_recent(ctx, e["id"])
    return e


@router.put("/employees/{eid}")
async def edit_employee(eid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    e = _clean_emp(data)
    if e.get("code"):
        await _check_code(ctx.cid, e["code"], eid)
    cur = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    if cur and "shift_id" in e and (e["shift_id"] or None) != (cur.get("shift_id") or None):
        # shift changed from the edit form: applies from today, earlier days keep the old shift
        from .shifts_roster import apply_shift_change
        await apply_shift_change(ctx, cur, e.pop("shift_id") or None, datetime.date.today().isoformat(), "edited")
    await db.employees.update_one({"id": eid, "company_id": ctx.cid}, {"$set": e})
    await _reprocess_recent(ctx, eid)
    return await db.employees.find_one({"id": eid}, {"_id": 0})


@router.delete("/employees/{eid}")
async def remove_employee(eid: str, ctx: Ctx = Depends(company_ctx), permanent: int = 0, punches: int = 0, ignore: int = 0):
    """Default: mark as left (history kept). permanent=1 deletes the employee and all their records."""
    if not permanent:
        await db.employees.update_one({"id": eid, "company_id": ctx.cid},
                                      {"$set": {"active": False, "leaving_date": datetime.date.today().isoformat()}})
        return {"ok": True}
    if ctx.role not in ("owner", "superadmin"):
        raise HTTPException(403, "Only the owner can delete employees permanently")
    n = await _purge_employees(ctx, [eid], bool(punches), bool(ignore))
    return {"ok": True, "deleted": n}


@router.post("/employees/bulk-delete")
async def bulk_delete(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    ids = data.get("ids") or []
    if not ids:
        raise HTTPException(400, "Select employees first")
    if not data.get("permanent"):
        r = await db.employees.update_many({"company_id": ctx.cid, "id": {"$in": ids}},
                                           {"$set": {"active": False, "leaving_date": datetime.date.today().isoformat()}})
        return {"ok": True, "marked_left": r.modified_count}
    n = await _purge_employees(ctx, ids, bool(data.get("punches")), bool(data.get("ignore")))
    return {"ok": True, "deleted": n}


async def _purge_employees(ctx, ids, with_punches, ignore_ids):
    emps = await db.employees.find({"company_id": ctx.cid, "id": {"$in": ids}}, {"_id": 0}).to_list(10000)
    if not emps:
        return 0
    codes = []
    for e in emps:
        codes += [e["code"]] + list(e.get("extra_codes") or [])
    eids = [e["id"] for e in emps]
    for coll in (db.attendance, db.leaves, db.advances, db.payslips):
        await coll.delete_many({"company_id": ctx.cid, "employee_id": {"$in": eids}})
    await db.employees.delete_many({"company_id": ctx.cid, "id": {"$in": eids}})
    if with_punches:
        await purge_punches(ctx.company, {"emp_code": {"$in": codes}})
    if ignore_ids:
        await db.companies.update_one({"id": ctx.cid}, {"$addToSet": {"ignored_codes": {"$each": codes}}})
        await db.device_users.delete_many({"company_id": ctx.cid, "user_code": {"$in": codes}})
    return len(eids)


@router.post("/device-users/remove")
async def remove_device_users(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Remove IDs from the 'From machines' list. ignore=true keeps them hidden on future syncs."""
    codes = [str(c) for c in data.get("codes") or []]
    await db.device_users.delete_many({"company_id": ctx.cid, "user_code": {"$in": codes}})
    if data.get("ignore", True):
        await db.companies.update_one({"id": ctx.cid}, {"$addToSet": {"ignored_codes": {"$each": codes}}})
    return {"ok": True, "removed": len(codes)}


@router.get("/ignored-codes")
async def ignored_codes(ctx: Ctx = Depends(company_ctx)):
    return sorted(ctx.company.get("ignored_codes") or [], key=lambda c: (len(c), c))


@router.post("/ignored-codes/restore")
async def restore_codes(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    codes = [str(c) for c in data.get("codes") or []]
    await db.companies.update_one({"id": ctx.cid}, {"$pull": {"ignored_codes": {"$in": codes}}})
    await db.deleted_punches.delete_many({"company_id": ctx.cid, "emp_code": {"$in": codes}})
    return {"ok": True, "message": "Restored. Run Sync all with history to bring their punches back."}


async def _reprocess_recent(ctx, eid):
    today = datetime.date.today()
    first = today.replace(day=1) - datetime.timedelta(days=31)
    await process_range(ctx.company, first, today, [eid])


@router.get("/device-users")
async def device_users(ctx: Ctx = Depends(company_ctx)):
    """IDs that exist on machines / have punched but are not yet added as employees."""
    codes = set()
    async for e in db.employees.find({"company_id": ctx.cid}, {"code": 1, "extra_codes": 1}):
        codes.add(e.get("code"))
        codes.update(e.get("extra_codes") or [])
    rows = await db.device_users.find({"company_id": ctx.cid}, {"_id": 0}).to_list(10000)
    rows = [r for r in rows if r["user_code"] not in codes]
    key = await photo_key(ctx.company)
    for r in rows:
        r["photo_url"] = photo_url(key, r["user_code"], r.get("photo_v"))
    rows.sort(key=lambda r: (len(r["user_code"]), r["user_code"]))
    return rows


@router.post("/device-users/import")
async def import_device_users(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    codes = data.get("codes") or []
    defaults = _clean_emp(data.get("defaults") or {})
    added = 0
    for c in codes:
        du = await db.device_users.find_one({"company_id": ctx.cid, "user_code": c}, {"_id": 0})
        if not du or await db.employees.find_one({"company_id": ctx.cid, "code": c}):
            continue
        e = dict(salary_type="monthly", earnings=[{"name": "Basic", "amount": 0}], ot_allowed=True, active=True)
        e.update(defaults)
        e.update(id=new_id(), company_id=ctx.cid, code=c, name=du.get("name") or f"Employee {c}", created_at=utcnow_iso())
        if du.get("photo_v"):
            e["photo_v"] = du["photo_v"]
        await db.employees.insert_one(e)
        added += 1
    await db.device_users.delete_many({"company_id": ctx.cid, "user_code": {"$in": codes}})
    today = datetime.date.today()
    await process_range(ctx.company, today.replace(day=1) - datetime.timedelta(days=31), today)
    return {"added": added}


@router.post("/employees/import-csv")
async def import_csv(file: UploadFile = File(...), ctx: Ctx = Depends(company_ctx)):
    """Columns: code,name,phone,department,designation,salary_type,salary,daily_rate,joining_date"""
    text = (await file.read()).decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    added, updated, errors = 0, 0, []
    for i, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        code, name = row.get("code"), row.get("name")
        if not code or not name:
            errors.append(f"Row {i}: code and name required")
            continue
        e = {"code": code, "name": name, "phone": clean_phone(row.get("phone")), "department": row.get("department", ""),
             "designation": row.get("designation", ""), "salary_type": row.get("salary_type") or "monthly"}
        if row.get("salary"):
            e["earnings"] = [{"name": "Basic", "amount": float(row["salary"] or 0)}]
        if row.get("daily_rate"):
            e["daily_rate"] = float(row["daily_rate"])
        if row.get("joining_date"):
            e["joining_date"] = row["joining_date"]
        ex = await db.employees.find_one({"company_id": ctx.cid, "code": code})
        if ex:
            await db.employees.update_one({"id": ex["id"]}, {"$set": e})
            updated += 1
        else:
            e.update(id=new_id(), company_id=ctx.cid, active=True, ot_allowed=True, created_at=utcnow_iso())
            e.setdefault("earnings", [{"name": "Basic", "amount": 0}])
            await db.employees.insert_one(e)
            added += 1
    return {"added": added, "updated": updated, "errors": errors}


# ---------------- shifts ----------------
@router.get("/shifts")
async def list_shifts(ctx: Ctx = Depends(company_ctx)):
    return await db.shifts.find({"company_id": ctx.cid}, {"_id": 0}).to_list(200)


SHIFT_FIELDS = ("name", "start", "end", "grace_minutes", "early_grace_minutes", "full_day_hours", "half_day_hours",
                "break_minutes", "ot_after_minutes", "weekly_off", "is_default", "track_late", "track_ot", "present_on_punch",
                "flexible", "day_start")


def _flex_defaults(s):
    """Flexible shift: no fixed timing — first punch IN, last punch OUT, hours decide P / HD."""
    if s.get("flexible"):
        s.setdefault("start", "00:00")
        s.setdefault("end", "23:59")
        s["track_late"] = False
        s.setdefault("day_start", "04:00")
        s.setdefault("full_day_hours", 8)
        s.setdefault("half_day_hours", 4)
    return s


@router.post("/shifts")
async def add_shift(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    s = _flex_defaults({k: data[k] for k in SHIFT_FIELDS if k in data})
    s.update(id=new_id(), company_id=ctx.cid)
    if s.get("is_default"):
        await db.shifts.update_many({"company_id": ctx.cid}, {"$set": {"is_default": False}})
    await db.shifts.insert_one(dict(s))
    return s


@router.put("/shifts/{sid}")
async def edit_shift(sid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    s = _flex_defaults({k: data[k] for k in SHIFT_FIELDS if k in data})
    if s.get("is_default"):
        await db.shifts.update_many({"company_id": ctx.cid}, {"$set": {"is_default": False}})
    await db.shifts.update_one({"id": sid, "company_id": ctx.cid}, {"$set": s})
    return {"ok": True}


@router.delete("/shifts/{sid}")
async def del_shift(sid: str, ctx: Ctx = Depends(company_ctx)):
    if await db.employees.find_one({"company_id": ctx.cid, "shift_id": sid, "active": {"$ne": False}}):
        raise HTTPException(400, "Employees are using this shift. Move them to another shift first.")
    await db.shifts.delete_one({"id": sid, "company_id": ctx.cid})
    return {"ok": True}


# ---------------- holidays ----------------
@router.get("/holidays")
async def list_holidays(ctx: Ctx = Depends(company_ctx), year: str = None):
    q = {"company_id": ctx.cid}
    if year:
        q["date"] = {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}
    return await db.holidays.find(q, {"_id": 0}).sort("date", 1).to_list(500)


@router.post("/holidays")
async def add_holiday(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    h = {"id": new_id(), "company_id": ctx.cid, "date": data["date"], "name": data.get("name") or "Holiday"}
    await db.holidays.delete_many({"company_id": ctx.cid, "date": h["date"]})
    await db.holidays.insert_one(dict(h))
    d = datetime.date.fromisoformat(h["date"])
    await process_range(ctx.company, d, d)
    return h


@router.delete("/holidays/{hid}")
async def del_holiday(hid: str, ctx: Ctx = Depends(company_ctx)):
    h = await db.holidays.find_one({"id": hid, "company_id": ctx.cid})
    await db.holidays.delete_one({"id": hid, "company_id": ctx.cid})
    if h:
        d = datetime.date.fromisoformat(h["date"])
        await process_range(ctx.company, d, d)
    return {"ok": True}


# ---------------- leaves ----------------
@router.get("/leaves")
async def list_leaves(ctx: Ctx = Depends(company_ctx), month: str = None):
    q = {"company_id": ctx.cid}
    if month:
        q["date"] = {"$gte": f"{month}-01", "$lte": f"{month}-31"}
    rows = await db.leaves.find(q, {"_id": 0}).sort("date", -1).to_list(5000)
    return rows


@router.post("/leaves")
async def add_leave(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    emp = await db.employees.find_one({"id": data.get("employee_id"), "company_id": ctx.cid}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    d1 = datetime.date.fromisoformat(data["from_date"])
    d2 = datetime.date.fromisoformat(data.get("to_date") or data["from_date"])
    if d2 < d1 or (d2 - d1).days > 62:
        raise HTTPException(400, "Invalid date range")
    d = d1
    while d <= d2:
        await db.leaves.update_one({"company_id": ctx.cid, "employee_id": emp["id"], "date": d.isoformat()},
                                   {"$set": {"type": data.get("type") or "CL", "paid": bool(data.get("paid", True)),
                                             "reason": data.get("reason", ""), "employee_name": emp["name"]},
                                    "$setOnInsert": {"id": new_id()}}, upsert=True)
        d += datetime.timedelta(days=1)
    await process_range(ctx.company, d1, d2, [emp["id"]])
    return {"ok": True}


@router.delete("/leaves/{lid}")
async def del_leave(lid: str, ctx: Ctx = Depends(company_ctx)):
    lv = await db.leaves.find_one({"id": lid, "company_id": ctx.cid})
    await db.leaves.delete_one({"id": lid, "company_id": ctx.cid})
    if lv:
        d = datetime.date.fromisoformat(lv["date"])
        await process_range(ctx.company, d, d, [lv["employee_id"]])
    return {"ok": True}


# ---------------- advances ----------------
@router.get("/advances")
async def list_advances(employee_id: str = "", open_only: bool = False, ctx: Ctx = Depends(company_ctx)):
    q = {"company_id": ctx.cid}
    if employee_id:
        q["employee_id"] = employee_id
    if open_only:
        q["balance"] = {"$gt": 0}
    rows = await db.advances.find(q, {"_id": 0}).sort("date", -1).to_list(5000)
    for r in rows:
        r.setdefault("auto", True)
        r["recovered"] = round(float(r["amount"]) - float(r.get("balance") or 0), 2)
    return rows


def _installment(data):
    """Monthly installment is optional: blank/0 means 'ask me every month at salary time'."""
    inst = float(data.get("installment") or 0)
    return (inst, True) if inst > 0 else (0.0, False)


@router.post("/advances")
async def add_advance(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    emp = await db.employees.find_one({"id": data.get("employee_id"), "company_id": ctx.cid}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    amt = round(float(data.get("amount") or 0), 2)
    if amt <= 0:
        raise HTTPException(400, "Amount required")
    inst, auto = _installment(data)
    a = {"id": new_id(), "company_id": ctx.cid, "employee_id": emp["id"], "employee_name": emp["name"],
         "emp_code": emp.get("code"), "date": data.get("date") or datetime.date.today().isoformat(), "amount": amt,
         "balance": amt, "installment": inst, "auto": auto, "note": data.get("note", ""), "recoveries": [],
         "created_at": utcnow_iso(), "created_by": ctx.user["name"]}
    await db.advances.insert_one(dict(a))
    return a


@router.put("/advances/{aid}")
async def edit_advance(aid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    a = await db.advances.find_one({"id": aid, "company_id": ctx.cid}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Advance not found")
    upd = {}
    if "installment" in data:
        upd["installment"], upd["auto"] = _installment(data)
    for k in ("note", "date"):
        if k in data:
            upd[k] = data[k]
    if "amount" in data:
        amt = round(float(data["amount"] or 0), 2)
        recovered = float(a["amount"]) - float(a.get("balance") or 0)
        if amt < recovered or amt <= 0:
            raise HTTPException(400, f"Amount cannot be less than what is already recovered ({recovered:g})")
        upd["amount"], upd["balance"] = amt, round(amt - recovered, 2)
    await db.advances.update_one({"id": aid}, {"$set": upd})
    return await db.advances.find_one({"id": aid}, {"_id": 0})


@router.post("/advances/{aid}/repay")
async def repay_advance(aid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Employee paid back in cash (outside salary)."""
    a = await db.advances.find_one({"id": aid, "company_id": ctx.cid}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Advance not found")
    amt = round(min(float(data.get("amount") or 0), float(a["balance"])), 2)
    if amt <= 0:
        raise HTTPException(400, "Amount required")
    await db.advances.update_one({"id": aid}, {"$set": {"balance": round(float(a["balance"]) - amt, 2)},
                                               "$push": {"recoveries": {"month": "", "amount": amt, "cash": True,
                                                                        "date": data.get("date") or datetime.date.today().isoformat()}}})
    return await db.advances.find_one({"id": aid}, {"_id": 0})


@router.delete("/advances/{aid}")
async def del_advance(aid: str, ctx: Ctx = Depends(company_ctx)):
    a = await db.advances.find_one({"id": aid, "company_id": ctx.cid})
    if a and a.get("recoveries"):
        raise HTTPException(400, "Recovery already done from salary — cannot delete")
    await db.advances.delete_one({"id": aid, "company_id": ctx.cid})
    return {"ok": True}
