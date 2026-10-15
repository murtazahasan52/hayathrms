"""Employee self-service: an employee logs in and sees only their own punches, attendance and salary slips.
Also the owner's side: give / change / remove an employee's login."""
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import Response
from ..db import db
from ..auth import employee_ctx, owner_ctx, hash_pw, Ctx
from ..defaults import company_settings
from ..utils import new_id, utcnow_iso, month_bounds, now_local
from ..services.attendance import process_range, STATUS_LABELS

router = APIRouter(prefix="/api")
EMP_PUBLIC = ("id", "code", "name", "department", "designation", "phone", "joining_date", "shift_id")


@router.get("/me/profile")
async def my_profile(ctx: Ctx = Depends(employee_ctx)):
    e = await db.employees.find_one({"id": ctx.user["employee_id"], "company_id": ctx.cid}, {"_id": 0})
    if not e:
        raise HTTPException(404, "Your employee record was removed. Contact the office.")
    sh = await db.shifts.find_one({"id": e.get("shift_id"), "company_id": ctx.cid}, {"_id": 0}) if e.get("shift_id") else None
    out = {k: e.get(k) for k in EMP_PUBLIC}
    out["shift"] = {k: (sh or {}).get(k) for k in ("name", "start", "end", "flexible", "full_day_hours")} if sh else None
    return {"employee": out, "company": {"name": ctx.company.get("name"), "logo": ctx.company.get("logo")},
            "valid_till": ctx.user.get("valid_till")}


@router.get("/me/attendance")
async def my_attendance(month: str = None, ctx: Ctx = Depends(employee_ctx)):
    eid = ctx.user["employee_id"]
    month = month or now_local(ctx.company).strftime("%Y-%m")
    d1, d2 = month_bounds(month)
    await process_range(ctx.company, d1, d2, [eid])
    rows = await db.attendance.find({"company_id": ctx.cid, "employee_id": eid,
                                     "date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}},
                                    {"_id": 0, "manual_remark": 0}).sort("date", 1).to_list(40)
    from ..services.payroll import summarize
    sm = summarize(rows, company_settings(ctx.company))
    for r in rows:
        r["status_label"] = STATUS_LABELS.get(r.get("status"), r.get("status"))
    totals = {"work_minutes": sum(r.get("work_minutes") or 0 for r in rows),
              "ot_minutes": sum(r.get("ot_minutes") or 0 for r in rows),
              "late_minutes": sum(r.get("late_minutes") or 0 for r in rows)}
    return {"month": month, "rows": rows, "summary": sm, "totals": totals}


@router.get("/me/payslips")
async def my_payslips(ctx: Ctx = Depends(employee_ctx)):
    rows = await db.payslips.find({"company_id": ctx.cid, "employee_id": ctx.user["employee_id"], "status": "final"},
                                  {"_id": 0, "id": 1, "month": 1, "gross": 1, "net": 1, "total_deductions": 1}).sort("month", -1).to_list(36)
    return rows


@router.get("/me/payslips/{sid}/pdf")
async def my_payslip_pdf(sid: str, ctx: Ctx = Depends(employee_ctx)):
    s = await db.payslips.find_one({"id": sid, "company_id": ctx.cid, "employee_id": ctx.user["employee_id"], "status": "final"}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Salary slip not found")
    from .work import _pdf_for
    pdf, _, _ = await _pdf_for(s)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Salary_Slip_{s["month"]}.pdf"'})


# ---------------- owner side ----------------
@router.get("/employees/{eid}/login")
async def get_emp_login(eid: str, ctx: Ctx = Depends(owner_ctx)):
    u = await db.users.find_one({"company_id": ctx.cid, "employee_id": eid, "role": "employee"},
                                {"_id": 0, "password_hash": 0})
    return {"login": u}


@router.put("/employees/{eid}/login")
async def set_emp_login(eid: str, data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    e = await db.employees.find_one({"id": eid, "company_id": ctx.cid}, {"_id": 0})
    if not e:
        raise HTTPException(404, "Employee not found")
    login_id = (data.get("login_id") or e.get("phone") or "").strip().lower().replace(" ", "")
    if not login_id:
        raise HTTPException(400, "Give a login ID (mobile number or email)")
    u = await db.users.find_one({"company_id": ctx.cid, "employee_id": eid, "role": "employee"}, {"_id": 0})
    other = await db.users.find_one({"email": login_id}, {"_id": 0, "id": 1})
    if other and (not u or other["id"] != u["id"]):
        raise HTTPException(400, "This login ID is already used by someone else")
    vt = data.get("valid_till") or None
    if data.get("months"):
        vt = (datetime.date.today() + datetime.timedelta(days=round(30.44 * int(data["months"])))).isoformat()
    if u:
        upd = {"email": login_id, "name": e["name"], "active": data.get("active", True)}
        if "valid_till" in data or data.get("months"):
            upd["valid_till"] = vt
        if data.get("password"):
            if len(data["password"]) < 6:
                raise HTTPException(400, "Password must be at least 6 characters")
            upd["password_hash"] = hash_pw(data["password"])
        await db.users.update_one({"id": u["id"]}, {"$set": upd, **({"$inc": {"token_version": 1}} if data.get("password") else {})})
    else:
        if len(data.get("password") or "") < 6:
            raise HTTPException(400, "Set a password of 6+ characters")
        await db.users.insert_one({"id": new_id(), "email": login_id, "name": e["name"], "role": "employee",
                                   "company_id": ctx.cid, "employee_id": eid, "active": True, "valid_till": vt,
                                   "password_hash": hash_pw(data["password"]), "created_at": utcnow_iso()})
    return await get_emp_login(eid, ctx)


@router.delete("/employees/{eid}/login")
async def del_emp_login(eid: str, ctx: Ctx = Depends(owner_ctx)):
    await db.users.delete_many({"company_id": ctx.cid, "employee_id": eid, "role": "employee"})
    return {"ok": True}
