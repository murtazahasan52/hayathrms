"""Attendance views & corrections, payroll, payslips, WhatsApp, reports."""
import csv
import io
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import Response, StreamingResponse
from ..db import db
from ..auth import company_ctx, owner_ctx, Ctx
from ..config import PUBLIC_BASE_URL
from ..defaults import whatsapp_settings, company_settings
from ..utils import new_id, token, utcnow_iso, month_bounds, now_local, fmt_hm, daterange
from ..services.attendance import process_range, set_manual_status, get_shifts, STATUS_LABELS
from ..services.ingest import ingest
from ..services.sync import purge_punches
from ..services.photos import photo_key, photo_url
from ..services.payroll import compute_slip, shift_hours_of
from ..services.payslip_pdf import payslip_pdf
from ..services import whatsapp, relay
from ..services.exports import Table, send
from ..services.scheduler import send_daily_summary, send_absent_alerts
from .devices import server_base

router = APIRouter(prefix="/api")


# ---------------- attendance ----------------
@router.get("/attendance/daily")
async def daily(date: str, ctx: Ctx = Depends(company_ctx)):
    d = datetime.date.fromisoformat(date)
    await process_range(ctx.company, d, d)
    rows = await db.attendance.find({"company_id": ctx.cid, "date": date}, {"_id": 0}).to_list(10000)
    rows.sort(key=lambda r: (len(r.get("emp_code") or ""), r.get("emp_code") or ""))
    await _attach_photos(ctx, rows)
    return {"rows": rows, "labels": STATUS_LABELS}


async def _attach_photos(ctx, rows):
    key = await photo_key(ctx.company)
    pv = {e["id"]: (e["code"], e.get("photo_v")) async for e in db.employees.find(
        {"company_id": ctx.cid, "photo_v": {"$exists": True}}, {"id": 1, "code": 1, "photo_v": 1})}
    for r in rows:
        x = pv.get(r.get("employee_id"))
        r["photo_url"] = photo_url(key, x[0], x[1]) if x else None


@router.post("/attendance/reprocess")
async def reprocess(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    d1 = datetime.date.fromisoformat(data["from_date"])
    d2 = datetime.date.fromisoformat(data.get("to_date") or data["from_date"])
    if (d2 - d1).days > 93:
        raise HTTPException(400, "Max 3 months at a time")
    n = await process_range(ctx.company, d1, d2)
    return {"ok": True, "days_processed": n}


@router.put("/attendance/manual")
async def manual_status(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    st = data.get("status") or None
    if st and st not in ("P", "A", "HD", "WO", "H", "L", "LWP"):
        raise HTTPException(400, "Invalid status")
    await set_manual_status(ctx.company, data["employee_id"], data["date"], st, data.get("remark", ""))
    return await db.attendance.find_one({"company_id": ctx.cid, "employee_id": data["employee_id"], "date": data["date"]}, {"_id": 0})


@router.post("/attendance/manual-punch")
async def manual_punch(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    emp = await db.employees.find_one({"id": data["employee_id"], "company_id": ctx.cid}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = f"{data['date']} {data['time']}:00"[:19]
    await ingest(ctx.company, None, [{"user_id": emp["code"], "ts": ts, "direction": data.get("direction")}], "manual")
    await db.punches.update_one({"company_id": ctx.cid, "emp_code": emp["code"], "ts": ts},
                                {"$set": {"added_by": ctx.user["name"], "note": data.get("note", "")}})
    return {"ok": True}


@router.delete("/punches/{pid}")
async def delete_punch(pid: str, ctx: Ctx = Depends(company_ctx)):
    p = await db.punches.find_one({"id": pid, "company_id": ctx.cid})
    if not p:
        raise HTTPException(404, "Punch not found")
    if p.get("source") != "manual" and ctx.role not in ("owner", "superadmin"):
        raise HTTPException(403, "Only the owner can delete machine punches")
    await purge_punches(ctx.company, {"id": pid})
    return {"ok": True}


@router.post("/punches/delete")
async def delete_punches(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    ids = data.get("ids") or []
    if not ids:
        raise HTTPException(400, "Select punches to delete")
    n = await purge_punches(ctx.company, {"id": {"$in": ids}})
    return {"ok": True, "deleted": n}


@router.post("/punches/delete-range")
async def delete_punch_range(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    """Delete all punches in a date range, optionally only one employee ID or one device. dry_run = count only."""
    d1, d2 = data.get("from_date"), data.get("to_date") or data.get("from_date")
    if not d1:
        raise HTTPException(400, "From date is required")
    q = {"company_id": ctx.cid, "date": {"$gte": d1, "$lte": d2}}
    if data.get("emp_code"):
        q["emp_code"] = str(data["emp_code"]).strip()
    if data.get("device_id"):
        q["device_id"] = data["device_id"]
    if data.get("dry_run"):
        return {"count": await db.punches.count_documents(q)}
    n = await purge_punches(ctx.company, q)
    return {"ok": True, "deleted": n}


async def _register(ctx, month):
    d1, d2 = month_bounds(month)
    await process_range(ctx.company, d1, d2)
    emps = await db.employees.find({"company_id": ctx.cid, "$or": [{"active": {"$ne": False}},
                                                                   {"leaving_date": {"$gte": d1.isoformat()}}]},
                                   {"_id": 0}).to_list(10000)
    emps.sort(key=lambda e: (len(e.get("code", "")), e.get("code", "")))
    att = {}
    async for a in db.attendance.find({"company_id": ctx.cid, "date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}}, {"_id": 0}):
        att[(a["employee_id"], a["date"])] = a
    days = [d.isoformat() for d in daterange(d1, d2)]
    return emps, att, days


@router.get("/attendance/monthly")
async def monthly(month: str, ctx: Ctx = Depends(company_ctx)):
    emps, att, days = await _register(ctx, month)
    out = []
    from ..services.payroll import summarize
    settings = company_settings(ctx.company)
    for e in emps:
        rows = [att.get((e["id"], d)) for d in days]
        cells = [{"s": r["status"], "in": r.get("in_time"), "out": r.get("out_time"), "late": r.get("late_minutes"),
                  "m": bool(r.get("manual_status"))} if r else None for r in rows]
        sm = summarize([r for r in rows if r], settings)
        out.append({"employee": {k: e.get(k) for k in ("id", "code", "name", "department")}, "cells": cells, "summary": sm})
    return {"days": days, "rows": out}


@router.get("/attendance/employee/{eid}")
async def emp_month(eid: str, month: str, ctx: Ctx = Depends(company_ctx)):
    d1, d2 = month_bounds(month)
    await process_range(ctx.company, d1, d2, [eid])
    rows = await db.attendance.find({"company_id": ctx.cid, "employee_id": eid,
                                     "date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}}, {"_id": 0}).sort("date", 1).to_list(40)
    emp = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    codes = [emp["code"]] + (emp.get("extra_codes") or []) if emp else []
    punches = await db.punches.find({"company_id": ctx.cid, "emp_code": {"$in": codes},
                                     "date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}}, {"_id": 0}).sort("ts", 1).to_list(3000)
    return {"employee": emp, "rows": rows, "punches": punches}


# ---------------- payroll ----------------
async def _build_slip(ctx, emp, month, existing=None):
    d1, d2 = month_bounds(month)
    rows = await db.attendance.find({"company_id": ctx.cid, "employee_id": emp["id"],
                                     "date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}}, {"_id": 0}).to_list(40)
    advances = await db.advances.find({"company_id": ctx.cid, "employee_id": emp["id"], "balance": {"$gt": 0},
                                       "date": {"$lte": d2.isoformat()}}, {"_id": 0}).to_list(50)
    shifts, default = await get_shifts(ctx.cid)
    sh = shifts.get(emp.get("shift_id")) or default
    adj = (existing or {}).get("adjustments") or {}
    slip = compute_slip(ctx.company, emp, month, rows, advances, adj, shift_hours_of(sh))
    slip.update(employee_id=emp["id"], employee_name=emp["name"], emp_code=emp.get("code"),
                department=emp.get("department", ""), designation=emp.get("designation", ""), phone=emp.get("phone", ""),
                bank_account=emp.get("bank_account", ""), ifsc=emp.get("ifsc", ""))
    return slip


@router.post("/payroll/generate")
async def generate(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    month = data["month"]
    d1, d2 = month_bounds(month)
    if d1 > now_local(ctx.company).date():
        raise HTTPException(400, "Cannot generate salary for a future month")
    await process_range(ctx.company, d1, d2)
    q = {"company_id": ctx.cid, "$or": [{"active": {"$ne": False}}, {"leaving_date": {"$gte": d1.isoformat()}}]}
    if data.get("employee_ids"):
        q["id"] = {"$in": data["employee_ids"]}
    emps = await db.employees.find(q, {"_id": 0}).to_list(10000)
    made, skipped = 0, 0
    for e in emps:
        if (e.get("joining_date") or "") > d2.isoformat():
            continue
        ex = await db.payslips.find_one({"company_id": ctx.cid, "month": month, "employee_id": e["id"]}, {"_id": 0})
        if ex and ex.get("status") == "final":
            skipped += 1
            continue
        slip = await _build_slip(ctx, e, month, ex)
        slip.update(status="draft", updated_at=utcnow_iso())
        if ex:
            await db.payslips.update_one({"id": ex["id"]}, {"$set": slip})
        else:
            slip.update(id=new_id(), company_id=ctx.cid, token=token(20), created_at=utcnow_iso())
            await db.payslips.insert_one(slip)
        made += 1
    return {"ok": True, "generated": made, "locked_skipped": skipped}


@router.get("/payroll")
async def list_payroll(month: str, ctx: Ctx = Depends(company_ctx)):
    rows = await db.payslips.find({"company_id": ctx.cid, "month": month}, {"_id": 0}).to_list(10000)
    rows.sort(key=lambda r: (len(r.get("emp_code") or ""), r.get("emp_code") or ""))
    await _attach_photos(ctx, rows)
    tot = {"gross": sum(r["gross"] for r in rows), "deductions": sum(r["total_deductions"] for r in rows),
           "net": sum(r["net"] for r in rows), "count": len(rows),
           "final": sum(1 for r in rows if r.get("status") == "final")}
    return {"rows": rows, "totals": tot}


@router.put("/payroll/{sid}/adjust")
async def adjust(sid: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    ex = await db.payslips.find_one({"id": sid, "company_id": ctx.cid}, {"_id": 0})
    if not ex:
        raise HTTPException(404, "Payslip not found")
    if ex.get("status") == "final":
        raise HTTPException(400, "Payslip is finalised. Unlock the month to edit.")
    emp = await db.employees.find_one({"id": ex["employee_id"]}, {"_id": 0})
    prev = ex.get("adjustments") or {}
    ex["adjustments"] = {"additions": data.get("additions", prev.get("additions")) or [],
                         "deductions": data.get("deductions", prev.get("deductions")) or [],
                         "advance": _clean_adv(data["advance"]) if "advance" in data else (prev.get("advance") or {})}
    slip = await _build_slip(ctx, emp, ex["month"], ex)
    slip["updated_at"] = utcnow_iso()
    await db.payslips.update_one({"id": sid}, {"$set": slip})
    return await db.payslips.find_one({"id": sid}, {"_id": 0})


def _clean_adv(m):
    """{advance_id: amount}. Blank = use the automatic installment; 0 = skip this month."""
    out = {}
    for k, v in (m or {}).items():
        if v is None or v == "":
            continue
        out[str(k)] = max(0.0, round(float(v), 2))
    return out


@router.get("/payroll/advances")
async def payroll_advances(month: str, ctx: Ctx = Depends(company_ctx)):
    """Every open advance next to this month's salary slip, with what will be cut — for the 'Advance deductions' screen."""
    _, d2 = month_bounds(month)
    slips = {s["employee_id"]: s async for s in db.payslips.find({"company_id": ctx.cid, "month": month}, {"_id": 0})}
    out = []
    async for a in db.advances.find({"company_id": ctx.cid, "date": {"$lte": d2.isoformat()}}, {"_id": 0}).sort("date", 1):
        s = slips.get(a["employee_id"])
        taken = next((r["amount"] for r in (s or {}).get("advance_recoveries") or [] if r["advance_id"] == a["id"]), 0)
        if float(a.get("balance") or 0) <= 0 and not (s and s.get("status") == "final" and taken):
            continue
        chosen = ((s or {}).get("adjustments") or {}).get("advance") or {}
        out.append({"advance_id": a["id"], "employee_id": a["employee_id"], "employee_name": a["employee_name"],
                    "emp_code": a.get("emp_code"), "date": a["date"], "amount": a["amount"], "balance": a["balance"],
                    "installment": a.get("installment") or 0, "auto": a.get("auto", True), "note": a.get("note", ""),
                    "this_month": taken, "chosen": chosen.get(a["id"]), "slip_id": (s or {}).get("id"),
                    "slip_status": (s or {}).get("status"), "net": (s or {}).get("net")})
    return out


@router.post("/payroll/advance-deductions")
async def set_advance_deductions(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    """Set how much of each advance to cut from this month's salary: {month, amounts: {advance_id: amount|''}}."""
    month = data["month"]
    amounts = data.get("amounts") or {}
    ids = list(amounts.keys())
    advs = await db.advances.find({"company_id": ctx.cid, "id": {"$in": ids}}, {"_id": 0}).to_list(5000)
    by_emp = {}
    for a in advs:
        by_emp.setdefault(a["employee_id"], {})[a["id"]] = amounts.get(a["id"])
    updated, missing, locked = 0, 0, 0
    for eid, m in by_emp.items():
        ex = await db.payslips.find_one({"company_id": ctx.cid, "month": month, "employee_id": eid}, {"_id": 0})
        if not ex:
            missing += 1
            continue
        if ex.get("status") == "final":
            locked += 1
            continue
        adj = dict(ex.get("adjustments") or {})
        cur = dict(adj.get("advance") or {})
        for aid, v in m.items():
            if v is None or v == "":
                cur.pop(aid, None)
            else:
                cur[aid] = max(0.0, round(float(v), 2))
        adj["advance"] = cur
        ex["adjustments"] = adj
        emp = await db.employees.find_one({"id": eid}, {"_id": 0})
        slip = await _build_slip(ctx, emp, month, ex)
        slip["updated_at"] = utcnow_iso()
        await db.payslips.update_one({"id": ex["id"]}, {"$set": slip})
        updated += 1
    return {"ok": True, "updated": updated, "no_slip": missing, "locked": locked}


@router.post("/payroll/finalize")
async def finalize(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    month = data["month"]
    n = 0
    async for s in db.payslips.find({"company_id": ctx.cid, "month": month, "status": "draft"}, {"_id": 0}):
        for rec in s.get("advance_recoveries") or []:
            a = await db.advances.find_one({"id": rec["advance_id"]})
            if a:
                bal = max(0.0, round(float(a["balance"]) - float(rec["amount"]), 2))
                await db.advances.update_one({"id": a["id"]}, {"$set": {"balance": bal},
                                                               "$push": {"recoveries": {"month": month, "amount": rec["amount"], "payslip_id": s["id"]}}})
        await db.payslips.update_one({"id": s["id"]}, {"$set": {"status": "final", "finalized_at": utcnow_iso(),
                                                                "finalized_by": ctx.user["name"]}})
        n += 1
    return {"ok": True, "finalized": n}


@router.post("/payroll/unlock")
async def unlock(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    month = data["month"]
    n = 0
    async for s in db.payslips.find({"company_id": ctx.cid, "month": month, "status": "final"}, {"_id": 0}):
        for rec in s.get("advance_recoveries") or []:
            a = await db.advances.find_one({"id": rec["advance_id"]})
            if a:
                await db.advances.update_one({"id": a["id"]}, {"$set": {"balance": round(float(a["balance"]) + float(rec["amount"]), 2)},
                                                               "$pull": {"recoveries": {"payslip_id": s["id"]}}})
        await db.payslips.update_one({"id": s["id"]}, {"$set": {"status": "draft"}})
        n += 1
    return {"ok": True, "unlocked": n}


@router.delete("/payroll/month")
async def delete_payroll_month(month: str, ctx: Ctx = Depends(owner_ctx)):
    if await db.payslips.count_documents({"company_id": ctx.cid, "month": month, "status": "final"}):
        raise HTTPException(400, "This month is finalised. Unlock it first, then delete.")
    r = await db.payslips.delete_many({"company_id": ctx.cid, "month": month})
    return {"ok": True, "deleted": r.deleted_count}


@router.delete("/payroll/{sid}")
async def delete_payslip(sid: str, ctx: Ctx = Depends(owner_ctx)):
    s = await db.payslips.find_one({"id": sid, "company_id": ctx.cid})
    if not s:
        raise HTTPException(404, "Payslip not found")
    if s.get("status") == "final":
        raise HTTPException(400, "Payslip is finalised. Unlock the month first.")
    await db.payslips.delete_one({"id": sid})
    return {"ok": True}


@router.get("/company/sample-payslip.pdf")
async def sample_payslip(ctx: Ctx = Depends(company_ctx)):
    """Preview of how slips look with the current logo/profile: latest real slip if any, else a sample."""
    slip = await db.payslips.find_one({"company_id": ctx.cid}, {"_id": 0}, sort=[("month", -1)])
    if slip:
        pdf, _, _ = await _pdf_for(slip)
    else:
        from ..services.payroll import compute_slip
        emp = {"id": "sample", "name": "Sample Employee", "code": "101", "designation": "Operator", "department": "Production",
               "joining_date": "2025-04-01", "phone": "9800000000",
               "earnings": [{"name": "Basic", "amount": 12000}, {"name": "HRA", "amount": 3000}, {"name": "Allowance", "amount": 1000}]}
        month = (now_local(ctx.company).date().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        d1, d2 = month_bounds(month)
        rows = [{"date": d.isoformat(), "status": "WO" if d.weekday() == 6 else "P", "day_type": "WO" if d.weekday() == 6 else "W",
                 "work_minutes": 0 if d.weekday() == 6 else 540, "ot_minutes": 0, "late_minutes": 0} for d in daterange(d1, d2)]
        slip = compute_slip(ctx.company, emp, month, rows, [], {}, 9)
        slip.update(id="SAMPLE", employee_name=emp["name"], emp_code="101", status="draft")
        pdf = payslip_pdf(ctx.company, emp, slip)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="Sample_Salary_Slip.pdf"'})


async def _pdf_for(slip):
    company = await db.companies.find_one({"id": slip["company_id"]}, {"_id": 0})
    emp = await db.employees.find_one({"id": slip["employee_id"]}, {"_id": 0}) or {}
    ph = await db.photos.find_one({"company_id": slip["company_id"], "code": emp.get("code")}, {"data": 1}) if emp else None
    if ph:
        emp = dict(emp, _photo=ph["data"])
    return payslip_pdf(company, emp, slip), company, emp


@router.get("/payroll/{sid}/pdf")
async def slip_pdf(sid: str, ctx: Ctx = Depends(company_ctx)):
    s = await db.payslips.find_one({"id": sid, "company_id": ctx.cid}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Payslip not found")
    pdf, _, emp = await _pdf_for(s)
    fn = f"Payslip_{s['month']}_{(s.get('emp_code') or '')}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{fn}"'})


@router.get("/public/payslip/{tok}")
async def public_slip(tok: str):
    tok = tok.replace(".pdf", "")
    s = await db.payslips.find_one({"token": tok}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Link expired or invalid")
    pdf, _, _ = await _pdf_for(s)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Salary_Slip_{s["month"]}.pdf"'})


@router.post("/payroll/send-whatsapp")
async def send_slips(request: Request, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    month = data["month"]
    q = {"company_id": ctx.cid, "month": month}
    if data.get("slip_ids"):
        q["id"] = {"$in": data["slip_ids"]}
    base = server_base(request)
    month_label = datetime.date.fromisoformat(month + "-01").strftime("%B %Y")
    results = []
    total_net = 0
    async for s in db.payslips.find(q, {"_id": 0}):
        total_net += s["net"]
        if not s.get("phone"):
            results.append({"employee": s["employee_name"], "status": "failed", "response": "No mobile number"})
            continue
        values = {"name": s["employee_name"], "month": month_label, "net": f"Rs. {s['net']:.0f}", "company": ctx.company["name"]}
        retry = {"kind": "payslip", "slip_id": s["id"], "base": base, "values": values}
        try:
            if not whatsapp_settings(ctx.company).get("enabled"):
                link = f"{base}/api/public/payslip/{s['token']}.pdf"   # WhatsApp off: nothing is uploaded
            else:
                link = await relay.payslip_link(s, base)   # own public link, or put online through the Hayat relay
        except relay.Offline:
            log = await whatsapp.queue(ctx.company, "payslip", s["phone"], dict(values, link="(link)"),
                                       employee_id=s["employee_id"], retry=retry)
            await db.payslips.update_one({"id": s["id"]}, {"$set": {"whatsapp_status": "queued", "whatsapp_at": utcnow_iso()}})
            results.append({"employee": s["employee_name"], "status": "queued", "response": log["response"]})
            continue
        except relay.RelayError as e:
            results.append({"employee": s["employee_name"], "status": "failed", "response": str(e)})
            continue
        log = await whatsapp.send_event(ctx.company, "payslip", s["phone"], dict(values, link=link),
                                        employee_id=s["employee_id"], media_url=link, force=True, retry=retry)
        await db.payslips.update_one({"id": s["id"]}, {"$set": {"whatsapp_status": log["status"], "whatsapp_at": utcnow_iso()}})
        results.append({"employee": s["employee_name"], "status": log["status"], "response": log.get("response")})
    if data.get("notify_owner", True) and not data.get("slip_ids"):
        for ph in whatsapp.owner_numbers(ctx.company):
            await whatsapp.send_event(ctx.company, "monthly_summary", ph,
                                      {"month": month_label, "employees": len(results), "total_net": f"Rs. {total_net:.0f}",
                                       "company": ctx.company["name"]})
    return {"results": results, "sent": sum(1 for r in results if r["status"] == "sent")}


# ---------------- WhatsApp ----------------
@router.get("/whatsapp/settings")
async def wa_settings(ctx: Ctx = Depends(owner_ctx)):
    return whatsapp_settings(ctx.company)


@router.put("/whatsapp/settings")
async def wa_save(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    cur = whatsapp_settings(ctx.company)
    for k, v in data.items():
        if k == "events":
            for ek, ev in (v or {}).items():
                cur["events"].setdefault(ek, {}).update(ev or {})
        elif k == "password" and not v:
            continue
        elif k in cur:
            cur[k] = v
    await db.companies.update_one({"id": ctx.cid}, {"$set": {"whatsapp": cur}})
    return cur


@router.post("/whatsapp/test")
async def wa_test(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    return await whatsapp.send_template(ctx.company, data.get("phone"), data.get("template"), data.get("params") or "",
                                        event="test", media_url=data.get("media_url") or None)


@router.get("/whatsapp/connection")
async def wa_connection(request: Request, ctx: Ctx = Depends(owner_ctx)):
    """Is this install able to send WhatsApp (internet) and salary-slip PDFs (public link or relay)?"""
    import httpx
    base = (PUBLIC_BASE_URL or server_base(request)).rstrip("/")
    out = {"public": relay.is_public(base), "base": base, "relay": await relay.relay_server(), "internet": False,
           "relay_ok": False, "queued": await db.whatsapp_logs.count_documents({"company_id": ctx.cid, "status": "queued"})}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(out["relay"] + "/api/health", params={"db_check": 0})
        out["internet"] = True
        out["relay_ok"] = r.status_code == 200
    except Exception as e:
        out["error"] = e.__class__.__name__
    out["has_key"] = bool(await relay.relay_key())
    if out["public"]:
        out["message"] = "This server is on the internet — salary slip PDFs use its own link."
    elif not out["internet"]:
        out["message"] = "No internet on this computer right now. Messages wait in the outbox and go out when it is back."
    elif not out["has_key"]:
        out["message"] = "Internet is fine, but this computer has no licence key, so salary slip PDFs cannot be put online."
    elif not out["relay_ok"]:
        out["message"] = "Internet is fine, but the Hayat PDF link server is not answering. Text messages still go out."
    else:
        out["message"] = "Ready: text messages go out directly, salary slip PDFs are put online through the Hayat link server."
    return out


@router.post("/whatsapp/retry")
async def wa_retry(ctx: Ctx = Depends(owner_ctx)):
    await db.whatsapp_logs.update_many({"company_id": ctx.cid, "status": "queued"}, {"$set": {"next_try_at": ""}})
    n = await whatsapp.retry_queued(ctx.company, limit=200)
    left = await db.whatsapp_logs.count_documents({"company_id": ctx.cid, "status": "queued"})
    return {"ok": True, "sent": n, "waiting": left}


@router.post("/whatsapp/run")
async def wa_run(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    day = datetime.date.fromisoformat(data.get("date") or now_local(ctx.company).date().isoformat())
    if data.get("event") == "daily_summary":
        res = await send_daily_summary(ctx.company, day)
    elif data.get("event") == "absent_alert":
        res = await send_absent_alerts(ctx.company, day)
    else:
        raise HTTPException(400, "Unknown event")
    res = [r for r in res if r]
    return {"count": len(res), "results": res}


@router.post("/whatsapp/broadcast")
async def wa_broadcast(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    q = {"company_id": ctx.cid, "active": {"$ne": False}}
    if data.get("employee_ids"):
        q["id"] = {"$in": data["employee_ids"]}
    out = []
    async for e in db.employees.find(q, {"_id": 0}):
        if not e.get("phone"):
            continue
        params = whatsapp.render_params(data.get("params") or "", {"name": e["name"], "code": e["code"], "company": ctx.company["name"]})
        out.append(await whatsapp.send_template(ctx.company, e["phone"], data.get("template"), params, event="broadcast", employee_id=e["id"]))
    return {"count": len(out), "sent": sum(1 for r in out if r["status"] == "sent")}


@router.delete("/whatsapp/logs")
async def wa_clear_logs(ctx: Ctx = Depends(owner_ctx)):
    r = await db.whatsapp_logs.delete_many({"company_id": ctx.cid})
    return {"ok": True, "deleted": r.deleted_count}


@router.get("/whatsapp/logs")
async def wa_logs(ctx: Ctx = Depends(company_ctx), limit: int = 300):
    return await db.whatsapp_logs.find({"company_id": ctx.cid}, {"_id": 0}).sort("created_at", -1).to_list(min(limit, 2000))


# ---------------- CSV reports ----------------
def _csv(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return StreamingResponse(iter([buf.getvalue().encode("utf-8-sig")]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _month_label(month):
    return datetime.date.fromisoformat(month + "-01").strftime("%B %Y")


LEGEND = "P Present · A Absent · HD Half day · MIS Single punch · WO Weekly off · H Holiday · L Paid leave · LWP Leave without pay"


@router.get("/reports/monthly-register.{fmt}")
async def rep_register(fmt: str, month: str, ctx: Ctx = Depends(company_ctx)):
    emps, att, days = await _register(ctx, month)
    from ..services.payroll import summarize
    settings = company_settings(ctx.company)
    dn = [d[8:] + "\n" + "MTWTFSS"[datetime.date.fromisoformat(d).weekday()] for d in days]
    head = ["Code", "Name"] + dn + ["Present", "Half", "Absent", "WO/Hol", "Leave", "Late", "OT hrs"]
    rows = []
    for e in emps:
        rr = [att.get((e["id"], d)) for d in days]
        sm = summarize([r for r in rr if r], settings)
        rows.append([e["code"], e["name"]] + [("MIS" if (r and r["status"] == "IN") else r["status"]) if r else "" for r in rr] +
                    [sm["present_days"], sm["HD"], sm["A"], sm["paid_off_days"], sm["L"], sm["late_days"],
                     round(sm["ot_minutes"] / 60, 2)])
    if fmt == "csv":
        head = ["Code", "Name"] + [d[8:] for d in days] + head[2 + len(days):]
    t = Table("Monthly Attendance Register", _month_label(month), head, rows,
              status_cols=range(2, 2 + len(days)), widths=[6, 20] + [3.6] * len(days) + [6.5] * 7, legend=LEGEND)
    return send(t, ctx.company, fmt, f"Attendance_Register_{month}")


@router.get("/reports/daily.{fmt}")
async def rep_daily(fmt: str, date: str, ctx: Ctx = Depends(company_ctx)):
    d = datetime.date.fromisoformat(date)
    await process_range(ctx.company, d, d)
    rows = await db.attendance.find({"company_id": ctx.cid, "date": date}, {"_id": 0}).to_list(10000)
    head = ["Code", "Name", "Department", "Shift", "In", "Out", "Work (h:m)", "Late (min)", "Early (min)", "OT (h:m)",
            "Status", "Remark", "All punches"]
    out = []
    for r in sorted(rows, key=lambda r: (len(r.get("emp_code") or ""), r.get("emp_code") or "")):
        out.append([r.get("emp_code"), r.get("employee_name"), r.get("department") or "", r.get("shift_name") or "",
                    r.get("in_time") or "", r.get("out_time") or "", fmt_hm(r.get("work_minutes")), r.get("late_minutes") or 0,
                    r.get("early_minutes") or 0, fmt_hm(r.get("ot_minutes")), STATUS_LABELS.get(r["status"], r["status"]),
                    r.get("remark") or "", " ".join(r.get("punches") or [])])
    t = Table("Daily Attendance", d.strftime("%A, %d %B %Y"), head, out,
              widths=[6, 20, 12, 12, 7, 7, 7, 6, 6, 7, 11, 14, 22])
    return send(t, ctx.company, fmt, f"Daily_Attendance_{date}")


@router.get("/reports/payroll.{fmt}")
async def rep_payroll(fmt: str, month: str, ctx: Ctx = Depends(company_ctx)):
    rows = await db.payslips.find({"company_id": ctx.cid, "month": month}, {"_id": 0}).to_list(10000)
    rows.sort(key=lambda r: (len(r.get("emp_code") or ""), r.get("emp_code") or ""))
    head = ["Code", "Name", "Department", "Paid days", "Present", "Absent", "Late", "OT hrs", "Gross", "Advance cut",
            "Other deductions", "Net pay", "Bank A/c", "IFSC", "Status"]
    out = []
    for r in rows:
        sm = r["summary"]
        adv = round(sum(x["amount"] for x in r.get("advance_recoveries") or []), 2)
        out.append([r.get("emp_code"), r["employee_name"], r.get("department") or "", r["payable_days"], sm["present_days"],
                    sm["A"], sm["late_days"], round(sm["ot_minutes"] / 60, 2), r["gross"], adv,
                    round(r["total_deductions"] - adv, 2), r["net"], r.get("bank_account", ""), r.get("ifsc", ""),
                    "Final" if r.get("status") == "final" else "Draft"])
    tot = ["", f"TOTAL ({len(rows)})", "", "", "", "", "", "", round(sum(x[8] for x in out), 2), round(sum(x[9] for x in out), 2),
           round(sum(x[10] for x in out), 2), round(sum(x[11] for x in out), 2), "", "", ""]
    t = Table("Salary Register", _month_label(month), head, out, total=tot, money_cols={8, 9, 10, 11},
              widths=[6, 20, 12, 6, 6, 6, 5, 6, 10, 9, 10, 10, 15, 11, 7])
    return send(t, ctx.company, fmt, f"Salary_Register_{month}")


@router.get("/reports/advances.{fmt}")
async def rep_advances(fmt: str, ctx: Ctx = Depends(company_ctx)):
    rows = await db.advances.find({"company_id": ctx.cid}, {"_id": 0}).sort("date", -1).to_list(10000)
    head = ["Date", "Code", "Name", "Advance", "Recovered", "Balance", "Monthly cut", "Note"]
    out = [[r["date"], r.get("emp_code") or "", r["employee_name"], r["amount"], round(r["amount"] - r["balance"], 2),
            r["balance"], (r.get("installment") or 0) if r.get("auto", True) else "Ask each month", r.get("note") or ""]
           for r in rows]
    tot = ["", "", "TOTAL", round(sum(x[3] for x in out), 2), round(sum(x[4] for x in out), 2),
           round(sum(x[5] for x in out), 2), "", ""]
    t = Table("Employee Advances", f"as on {now_local(ctx.company):%d %b %Y}", head, out, total=tot,
              money_cols={3, 4, 5}, widths=[10, 6, 20, 10, 10, 10, 12, 24], landscape=False)
    return send(t, ctx.company, fmt, "Advances")
