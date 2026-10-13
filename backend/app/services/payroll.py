"""Salary calculation from processed attendance."""
import math
from ..defaults import company_settings
from ..utils import month_bounds, hm


def _r(x):
    return round(float(x or 0), 2)


def summarize(rows, settings):
    mis_factor = {"P": 1.0, "HD": 0.5, "A": 0.0}.get(settings.get("single_punch_as", "HD"), 0.5)
    s = dict(P=0, HD=0, MIS=0, A=0, WO=0, H=0, L=0, LWP=0, late_days=0, late_minutes=0, early_days=0,
             ot_minutes=0, work_minutes=0, present_days=0.0, paid_off_days=0.0, paid_leave_days=0.0,
             off_day_work=0.0, holiday_days=0.0)
    for r in rows:
        st = r.get("status")
        if st == "IN":
            st = "MIS"
        if st == "-":
            continue
        if st in s:
            s[st] += 1
        s["work_minutes"] += r.get("work_minutes") or 0
        val = {"P": 1.0, "HD": 0.5, "MIS": mis_factor}.get(st, 0.0)
        if r.get("day_type") in ("WO", "H"):
            if st in ("P", "HD", "MIS"):
                s["paid_off_days"] += 1
                if r.get("day_type") == "H":
                    s["holiday_days"] += 1
                if settings.get("pay_off_day_work", True):
                    s["off_day_work"] += val
            elif st in ("WO", "H", "L"):
                s["paid_off_days"] += 1
                if st == "H" or r.get("day_type") == "H":
                    s["holiday_days"] += 1
        else:
            if st in ("P", "HD", "MIS"):
                s["present_days"] += val
                if (r.get("late_minutes") or 0) > 0:
                    s["late_days"] += 1
                    s["late_minutes"] += r["late_minutes"]
                if (r.get("early_minutes") or 0) > 0:
                    s["early_days"] += 1
                s["ot_minutes"] += r.get("ot_minutes") or 0
            elif st == "L":
                s["paid_leave_days"] += 1
            elif st in ("WO", "H"):  # manually marked
                s["paid_off_days"] += 1
                if st == "H":
                    s["holiday_days"] += 1
    n = int(settings.get("late_count_for_halfday") or 0)
    s["late_penalty_days"] = (s["late_days"] // n) * 0.5 if n else 0.0
    return s


def compute_slip(company, emp, month, rows, advances, adjustments=None, shift_hours=8.0):
    settings = company_settings(company)
    d1, d2 = month_bounds(month)
    days_in_month = (d2 - d1).days + 1
    basis = settings.get("salary_days_basis", "month")
    basis_days = days_in_month if basis == "month" else int(basis)
    sm = summarize(rows, settings)
    stype = emp.get("salary_type", "monthly")
    earnings_cfg = [e for e in (emp.get("earnings") or []) if float(e.get("amount") or 0)]
    gross_fixed = sum(float(e["amount"]) for e in earnings_cfg)
    earnings = []
    regular_days = 0.0

    if stype == "daily":
        rate = float(emp.get("daily_rate") or 0)
        per_day = rate
        regular_days = max(0.0, sm["present_days"] + sm["paid_leave_days"] + sm["holiday_days"] - sm["late_penalty_days"])
        earnings.append({"name": f"Wages ({regular_days:g} days x {rate:g})", "amount": _r(rate * regular_days)})
        gross_fixed = rate * days_in_month
    elif stype == "hourly":
        rate = float(emp.get("hourly_rate") or 0)
        hours = sm["work_minutes"] / 60
        per_day = rate * shift_hours
        regular_days = sm["present_days"]
        earnings.append({"name": f"Wages ({hours:.2f} hrs x {rate:g})", "amount": _r(rate * hours)})
        gross_fixed = per_day * days_in_month
    else:
        per_day = gross_fixed / basis_days if basis_days else 0
        regular_days = sm["present_days"] + sm["paid_off_days"] + sm["paid_leave_days"] - sm["late_penalty_days"]
        if basis != "month":
            # with a 26/30-day basis weekly offs are not paid separately
            regular_days = sm["present_days"] + sm["paid_leave_days"] + sm["holiday_days"] - sm["late_penalty_days"]
        regular_days = max(0.0, min(regular_days, float(basis_days)))
        factor = regular_days / basis_days if basis_days else 0
        for e in earnings_cfg:
            earnings.append({"name": e["name"], "amount": _r(float(e["amount"]) * factor)})

    extra_pay = 0.0
    if sm["off_day_work"] and stype != "hourly":
        extra_pay = _r(per_day * sm["off_day_work"])
        earnings.append({"name": f"Weekly-off / holiday work ({sm['off_day_work']:g} days)", "amount": extra_pay})

    ot_pay = 0.0
    if emp.get("ot_allowed", True) and sm["ot_minutes"]:
        rate = float(emp.get("ot_rate_per_hour") or 0) or (per_day / (shift_hours or 8)) * float(settings.get("ot_multiplier") or 1)
        ot_pay = _r(rate * sm["ot_minutes"] / 60)
        if ot_pay:
            earnings.append({"name": f"Overtime ({sm['ot_minutes'] / 60:.2f} hrs)", "amount": ot_pay})

    adj = adjustments or {}
    for a in adj.get("additions") or []:
        if float(a.get("amount") or 0):
            earnings.append({"name": a.get("name") or "Addition", "amount": _r(a["amount"]), "manual": True})

    gross = _r(sum(e["amount"] for e in earnings))
    deductions = []
    if emp.get("pf_enabled"):
        basic = next((e["amount"] for e in earnings if e["name"].lower().startswith("basic")), None)
        base = basic if basic is not None else gross - ot_pay
        cap = float(settings.get("pf_wage_cap") or 0)
        if cap:
            base = min(base, cap)
        pf = round(base * float(settings.get("pf_percent") or 0) / 100)
        if pf:
            deductions.append({"name": "Provident Fund", "amount": pf})
    if emp.get("esi_enabled") and gross_fixed <= float(settings.get("esi_gross_limit") or 21000):
        esi = math.ceil(gross * float(settings.get("esi_percent") or 0) / 100)
        if esi:
            deductions.append({"name": "ESI", "amount": esi})
    if float(emp.get("pt_amount") or 0) and gross > 0:
        deductions.append({"name": "Professional Tax", "amount": float(emp["pt_amount"])})
    for d in emp.get("other_deductions") or []:
        if float(d.get("amount") or 0):
            deductions.append({"name": d.get("name") or "Deduction", "amount": _r(d["amount"])})
    for d in adj.get("deductions") or []:
        if float(d.get("amount") or 0):
            deductions.append({"name": d.get("name") or "Deduction", "amount": _r(d["amount"]), "manual": True})
    # Advance recovery. Per advance: the amount typed for this month's slip wins (0 = skip this month);
    # otherwise the automatic monthly installment (only when the advance has one); otherwise nothing.
    adv_total = 0.0
    adv_lines = []
    chosen = {str(k): v for k, v in (adj.get("advance") or {}).items()}
    room = max(0.0, gross - sum(d["amount"] for d in deductions))  # never cut an advance below zero salary
    for a in advances:
        bal = float(a.get("balance") or 0)
        if a["id"] in chosen and chosen[a["id"]] not in (None, ""):
            want = float(chosen[a["id"]] or 0)
        elif a.get("auto", True) and float(a.get("installment") or 0) > 0:
            want = float(a["installment"])
        else:
            want = 0.0
        take = _r(max(0.0, min(bal, want, room - adv_total)))
        if take > 0:
            adv_total += take
            adv_lines.append({"advance_id": a["id"], "amount": take, "balance_before": _r(bal),
                              "date": a.get("date"), "note": a.get("note", "")})
    if adv_total:
        deductions.append({"name": "Advance recovery", "amount": _r(adv_total)})

    total_ded = _r(sum(d["amount"] for d in deductions))
    net = round(gross - total_ded)
    return {
        "month": month, "salary_type": stype, "days_in_month": days_in_month, "basis_days": basis_days,
        "fixed_gross": _r(gross_fixed), "per_day": _r(per_day), "payable_days": _r(regular_days + sm["off_day_work"]),
        "summary": sm, "earnings": earnings, "deductions": deductions, "gross": gross,
        "total_deductions": total_ded, "net": max(net, 0), "net_negative": net < 0,
        "advance_recoveries": adv_lines, "adjustments": adj,
        "advance_balance": _r(sum(float(a.get("balance") or 0) for a in advances) - adv_total),
    }


def shift_hours_of(shift):
    if not shift:
        return 8.0
    a, b = hm(shift["start"]), hm(shift["end"])
    mins = (b - a) % 1440 or 1440
    return max(1.0, (mins - (shift.get("break_minutes") or 0)) / 60)
