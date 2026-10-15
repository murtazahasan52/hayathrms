"""Turns raw punches into daily attendance (P / A / HD / MIS / WO / H / L / LWP)."""
import datetime
from pymongo import UpdateOne
from ..db import db, bulk_upsert
from ..defaults import company_settings, DEFAULT_SHIFT
from ..utils import hm, now_local, daterange, utcnow_iso

STATUS_LABELS = {
    "P": "Present", "A": "Absent", "HD": "Half Day", "MIS": "Missed Punch", "WO": "Weekly Off",
    "H": "Holiday", "L": "Paid Leave", "LWP": "Leave Without Pay", "IN": "Inside", "-": "Not yet",
}


async def get_shifts(cid):
    shifts = await db.shifts.find({"company_id": cid}, {"_id": 0}).to_list(500)
    by_id = {s["id"]: s for s in shifts}
    default = next((s for s in shifts if s.get("is_default")), shifts[0] if shifts else dict(DEFAULT_SHIFT, id=None))
    return by_id, default


def is_flexible(shift):
    """Flexible shift: no fixed IN/OUT time. First punch of the day = IN, last punch = OUT."""
    return bool(shift.get("flexible"))


def day_start_min(shift):
    return hm(shift.get("day_start") or "00:00")


def flex_date(ts: datetime.datetime, shift):
    """Punches before the shift's day-start time (e.g. 04:00) still belong to the previous working day."""
    if ts.hour * 60 + ts.minute < day_start_min(shift):
        return ts.date() - datetime.timedelta(days=1)
    return ts.date()


def is_overnight(shift):
    if is_flexible(shift):
        return False
    return hm(shift["end"]) <= hm(shift["start"])


def attendance_date(ts: datetime.datetime, shift):
    if is_overnight(shift):
        cutoff = min(hm(shift["end"]) + 240, hm(shift["start"]) - 60)
        if ts.hour * 60 + ts.minute < cutoff:
            return ts.date() - datetime.timedelta(days=1)
    return ts.date()


def _parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def shift_for(emp, ds, shifts, default_shift, roster=None):
    """Shift on a given date: roster entry for that day > dated shift change > employee's shift > default.
    Returns (shift, roster_value) where roster_value is None, 'OFF' or a shift id."""
    r = (roster or {}).get((emp["id"], ds))
    if r and r != "OFF" and r in shifts:
        return shifts[r], r
    sid = emp.get("shift_id")
    for h in sorted(emp.get("shift_history") or [], key=lambda x: x.get("from") or ""):
        if (h.get("from") or "") <= ds:
            sid = h.get("shift_id")
    return (shifts.get(sid) or default_shift), r


def _fmt(t, d):
    return t.strftime("%H:%M") if t.date() == d else t.strftime("%H:%M (+1)")


def compute_day(d, shift, plist, settings, *, holiday=None, leave=None, employee=None, now=None, roster=None):
    """plist: list of punch docs (ts string, direction). Returns computed fields.
    roster: None, 'OFF' (weekly off given in the roster) or a shift id (working day even if normally off)."""
    weekly_off = employee.get("weekly_off") if employee and employee.get("weekly_off") is not None else shift.get("weekly_off", [6])
    if roster == "OFF":
        is_off = True
    elif roster:
        is_off = False
    else:
        is_off = d.weekday() in (weekly_off or [])
    day_type = "H" if holiday else ("WO" if is_off else "W")
    gap = int(settings.get("punch_dedupe_minutes") or 0)

    items = sorted(((_parse_ts(p["ts"]), p.get("direction")) for p in plist), key=lambda x: x[0])
    times, dirs = [], []
    for t, dr in items:
        if times and (t - times[-1]).total_seconds() < gap * 60:
            continue
        times.append(t)
        dirs.append(dr)

    flexible = is_flexible(shift)
    if flexible:
        start_dt = datetime.datetime.combine(d, datetime.time()) + datetime.timedelta(minutes=day_start_min(shift))
        end_dt = start_dt + datetime.timedelta(days=1)
        shift_minutes = (shift.get("full_day_hours") or 8) * 60
    else:
        st_min, en_min = hm(shift["start"]), hm(shift["end"])
        start_dt = datetime.datetime.combine(d, datetime.time()) + datetime.timedelta(minutes=st_min)
        end_dt = datetime.datetime.combine(d, datetime.time()) + datetime.timedelta(minutes=en_min)
        if is_overnight(shift):
            end_dt += datetime.timedelta(days=1)
        shift_minutes = (end_dt - start_dt).total_seconds() / 60 - (shift.get("break_minutes") or 0)

    t_in = t_out = None
    if times:
        t_in = times[0]
        if len(times) > 1:
            t_out = times[-1]
        if settings.get("use_device_inout") and any(dirs) and not flexible:
            ins = [t for t, dr in zip(times, dirs) if dr == "in"]
            outs = [t for t, dr in zip(times, dirs) if dr == "out"]
            t_in = ins[0] if ins else t_in
            t_out = outs[-1] if outs else t_out
            if t_out and t_in and t_out <= t_in:
                t_out = None

    work = late = early = ot = 0
    if t_in and t_out:
        work = (t_out - t_in).total_seconds() / 60
        brk = shift.get("break_minutes") or 0
        if brk and work >= (shift.get("half_day_hours") or 4) * 60:
            work -= brk
    # inside time = sum of IN-OUT pairs (1st-2nd, 3rd-4th ...); the gaps between pairs are breaks
    pairs, inside = [], 0
    for i in range(0, len(times) - 1, 2):
        pairs.append([times[i], times[i + 1]])
        inside += (times[i + 1] - times[i]).total_seconds() / 60
    track_late = shift.get("track_late", True) and not flexible   # False = no fixed timing: never late / early
    track_ot = shift.get("track_ot", True)       # False = this shift never earns overtime
    if t_in and day_type == "W" and track_late:
        diff = (t_in - start_dt).total_seconds() / 60
        if diff > (shift.get("grace_minutes") or 0):
            late = diff
    if t_out and day_type == "W" and track_late:
        diff = (end_dt - t_out).total_seconds() / 60
        if diff > (shift.get("early_grace_minutes") or 0):
            early = diff
    if work and day_type == "W" and track_ot and (employee or {}).get("ot_allowed", True) \
            and (employee or {}).get("ot_mode", "hours") != "fixed":
        # flexible shifts (no fixed timing) earn OT only beyond the full-day hours
        basis = shift_minutes if track_late else (shift.get("full_day_hours") or 8) * 60
        extra = work - basis
        if extra >= (shift.get("ot_after_minutes") or 0):
            ot = extra

    now = now or datetime.datetime.now()
    is_today = d == now.date()
    day_over = now >= end_dt
    n = len(times)
    remark = ""
    if n == 0:
        if leave:
            status = "L" if leave.get("paid", True) else "LWP"
            remark = leave.get("type", "")
        elif day_type == "H":
            status = "H"
            remark = holiday.get("name", "")
        elif day_type == "WO":
            status = "WO"
        elif is_today and not day_over:
            status = "-"
        else:
            status = "A"
    elif not t_out:
        if is_today and not day_over:
            status = "IN"
        elif day_type != "W":
            status = day_type
            remark = "Single punch on off day"
        else:
            status = "MIS"
    else:
        full = (shift.get("full_day_hours") or 7) * 60
        half = (shift.get("half_day_hours") or 4) * 60
        if shift.get("present_on_punch"):  # IN and OUT punched = full day, hours not checked
            status = "P"
        elif work >= full:
            status = "P"
        elif work >= half:
            status = "HD"
        else:
            status = "A"
            remark = "Short hours"
    return {
        "day_type": day_type,
        "in_time": t_in.strftime("%H:%M") if t_in else None,
        "out_time": _fmt(t_out, d) if t_out else None,
        "punches": [t.strftime("%H:%M") if t.date() == d else t.strftime("%H:%M (+1)") for t in times],
        "punch_count": n,
        "pairs": [[_fmt(a, d), _fmt(b, d)] for a, b in pairs],
        "inside_minutes": round(inside),
        "out_minutes": round(max(0, (t_out - t_in).total_seconds() / 60 - inside)) if t_in and t_out and n % 2 == 0 else 0,
        "flexible": flexible,
        "work_minutes": round(work),
        "late_minutes": round(late),
        "early_minutes": round(early),
        "ot_minutes": round(ot),
        "auto_status": status,
        "worked_off": day_type != "W" and n >= 2,
        "remark": remark,
    }


async def process_range(company, d1, d2, employee_ids=None):
    cid = company["id"]
    settings = company_settings(company)
    now = now_local(company)
    if d2 > now.date():
        d2 = now.date()
    if d1 > d2:
        return 0
    q = {"company_id": cid, "active": {"$ne": False}}
    if employee_ids:
        q = {"company_id": cid, "id": {"$in": list(employee_ids)}}
    employees = await db.employees.find(q, {"_id": 0}).to_list(10000)
    if not employees:
        return 0
    shifts, default_shift = await get_shifts(cid)
    s1, s2 = d1.isoformat(), d2.isoformat()
    holidays = {h["date"]: h async for h in db.holidays.find({"company_id": cid, "date": {"$gte": s1, "$lte": s2}}, {"_id": 0})}
    leaves = {}
    async for lv in db.leaves.find({"company_id": cid, "date": {"$gte": s1, "$lte": s2}}, {"_id": 0}):
        leaves[(lv["employee_id"], lv["date"])] = lv
    manual = {}
    async for a in db.attendance.find({"company_id": cid, "date": {"$gte": s1, "$lte": s2},
                                       "manual_status": {"$nin": [None, ""]}}, {"_id": 0, "employee_id": 1, "date": 1, "manual_status": 1}):
        manual[(a["employee_id"], a["date"])] = a["manual_status"]
    roster = {}
    async for r in db.roster.find({"company_id": cid, "date": {"$gte": (d1 - datetime.timedelta(days=1)).isoformat(),
                                                                "$lte": (d2 + datetime.timedelta(days=1)).isoformat()}},
                                  {"_id": 0, "employee_id": 1, "date": 1, "shift_id": 1}):
        roster[(r["employee_id"], r["date"])] = r["shift_id"]

    code_to_emp = {}
    for e in employees:
        for c in [e.get("code")] + list(e.get("extra_codes") or []):
            if c:
                code_to_emp[str(c)] = e["id"]
    p1 = (d1 - datetime.timedelta(days=1)).isoformat()
    p2 = (d2 + datetime.timedelta(days=1)).isoformat()
    by_emp_day = {}
    emp_map = {e["id"]: e for e in employees}
    async for p in db.punches.find({"company_id": cid, "date": {"$gte": p1, "$lte": p2},
                                    "emp_code": {"$in": list(code_to_emp.keys())}}, {"_id": 0}):
        eid = code_to_emp.get(p["emp_code"])
        if not eid:
            continue
        pts = _parse_ts(p["ts"])
        prev_sh, _ = shift_for(emp_map[eid], (pts.date() - datetime.timedelta(days=1)).isoformat(), shifts, default_shift, roster)
        today_sh, _ = shift_for(emp_map[eid], pts.date().isoformat(), shifts, default_shift, roster)
        if is_flexible(today_sh):
            ad = flex_date(pts, today_sh)
        else:
            ad = attendance_date(pts, prev_sh) if is_overnight(prev_sh) else pts.date()
        if ad != pts.date() and not is_overnight(today_sh) and not is_flexible(today_sh):
            # morning punch after a night shift: belongs to today if it is close to today's day-shift start
            if pts.hour * 60 + pts.minute >= hm(today_sh["start"]) - 120:
                ad = pts.date()
        by_emp_day.setdefault((eid, ad.isoformat()), []).append(p)

    ops = []
    for e in employees:
        jd = e.get("joining_date") or "1900-01-01"
        ld = e.get("leaving_date") or "9999-12-31"
        for d in daterange(d1, d2):
            ds = d.isoformat()
            if ds < jd or ds > ld:
                continue
            sh, rv = shift_for(e, ds, shifts, default_shift, roster)
            res = compute_day(d, sh, by_emp_day.get((e["id"], ds), []), settings,
                              holiday=holidays.get(ds), leave=leaves.get((e["id"], ds)), employee=e, now=now, roster=rv)
            ms = manual.get((e["id"], ds))
            res["status"] = ms or res["auto_status"]
            res.update({"shift_id": sh.get("id"), "shift_name": sh.get("name"), "emp_code": e.get("code"),
                        "employee_name": e.get("name"), "department": e.get("department", ""),
                        "updated_at": utcnow_iso()})
            ops.append(UpdateOne({"company_id": cid, "employee_id": e["id"], "date": ds},
                                 {"$set": res},
                                 upsert=True))
    for i in range(0, len(ops), 1000):
        await bulk_upsert(db.attendance, ops[i:i + 1000])
    return len(ops)


async def set_manual_status(company, employee_id, date, status, remark=""):
    await db.attendance.update_one({"company_id": company["id"], "employee_id": employee_id, "date": date},
                                   {"$set": {"manual_status": status or None, "manual_remark": remark}},
                                   upsert=True)
    d = datetime.date.fromisoformat(date)
    await process_range(company, d, d, [employee_id])
