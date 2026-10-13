import copy

DEFAULT_SETTINGS = {
    "timezone": "Asia/Kolkata",
    "punch_dedupe_minutes": 2,          # punches closer than this are treated as one
    "use_device_inout": False,
    "auto_add_employees": False,        # machine users not in the app are added as employees on sync          # True = trust IN/OUT keys pressed on the machine
    "single_punch_as": "HD",            # how a day with one punch is paid: P / HD / A
    "salary_days_basis": "month",       # month (actual days) / 30 / 26
    "late_count_for_halfday": 3,        # every N late marks = half day cut (0 = off)
    "pay_off_day_work": True,           # extra day's pay for working on weekly-off / holiday
    "ot_multiplier": 1.0,
    "pf_percent": 12.0,
    "pf_wage_cap": 15000,
    "esi_percent": 0.75,
    "esi_gross_limit": 21000,
    "payslip_footer": "This is a computer-generated salary slip.",
    "payslip_signature": True,
}

# Recommended WhatsApp templates — written for Meta's UTILITY category (transactional, no promotion,
# no variable at the very start or end of the body). Submit these in the BhashSMS panel with the same
# names; the "params" order below fills {{1}}, {{2}}, ... in that order.
WHATSAPP_EVENTS = {
    "punch_alert": {
        "label": "Punch alert to employee (IN / OUT)", "to": "employee",
        "template": "hrms_punch_alert",
        "params": "{name},{direction},{time},{date},{company}",
        "header": "None",
        "body": "Dear {{1}}, your {{2}} punch has been recorded at {{3}} on {{4}} at {{5}}. "
                "If you did not make this entry, please inform your manager.",
        "footer": "Automated attendance alert",
        "samples": ["Rahul Sharma", "IN", "09:02 AM", "13-10-2026", "Sunrise Engineering Works"],
    },
    "late_alert": {
        "label": "Late arrival alert to employee", "to": "employee",
        "template": "hrms_late_arrival",
        "params": "{name},{date},{time},{late_minutes},{company}",
        "header": "None",
        "body": "Dear {{1}}, on {{2}} your check-in was recorded at {{3}}, which is {{4}} minutes after your shift "
                "start time at {{5}}. If this is incorrect, please inform your manager.",
        "footer": "Automated attendance alert",
        "samples": ["Rahul Sharma", "13-10-2026", "09:24 AM", "14", "Sunrise Engineering Works"],
    },
    "absent_alert": {
        "label": "Absent alert to employee", "to": "employee",
        "template": "hrms_absent_alert",
        "params": "{name},{date},{company}",
        "header": "None",
        "body": "Dear {{1}}, no attendance has been recorded for you on {{2}} at {{3}}. If you are on duty or on "
                "approved leave, please inform your manager so that your record can be corrected.",
        "footer": "Automated attendance alert",
        "samples": ["Rahul Sharma", "13-10-2026", "Sunrise Engineering Works"],
    },
    "daily_summary": {
        "label": "Daily attendance summary to owner", "to": "owner",
        "template": "hrms_daily_summary",
        "params": "{company},{date},{total},{present},{absent},{late},{absent_names}",
        "header": "Text: Daily attendance report",
        "body": "Attendance summary for {{1}} on {{2}}.\n\nTotal staff: {{3}}\nPresent: {{4}}\nAbsent: {{5}}\n"
                "Late: {{6}}\nAbsent staff: {{7}}\n\nThis report is generated automatically by your attendance system.",
        "footer": "Hayat HRMS",
        "samples": ["Sunrise Engineering Works", "13-10-2026", "12", "10", "2", "1", "Imran Sheikh | Rohit Gawande"],
    },
    "payslip": {
        "label": "Salary slip to employee (with PDF)", "to": "employee",
        "template": "hrms_salary_slip",
        "params": "{name},{month},{company},{net},{link}",
        "header": "Document (PDF) — sample file: any salary slip PDF",
        "body": "Dear {{1}}, your salary slip for {{2}} from {{3}} is attached. Net pay credited: {{4}}. "
                "You can also view it at {{5}} at any time. Please contact the accounts team for any questions.",
        "footer": "Automated payroll notification",
        "samples": ["Rahul Sharma", "September 2026", "Sunrise Engineering Works", "Rs. 18450",
                    "https://hrms.example.com/api/public/payslip/abc123.pdf"],
    },
    "monthly_summary": {
        "label": "Payroll summary to owner", "to": "owner",
        "template": "hrms_payroll_summary",
        "params": "{company},{month},{employees},{total_net}",
        "header": "None",
        "body": "Payroll for {{1}} for {{2}} has been processed for {{3}} employees. Total net payable: {{4}}. "
                "Salary slips have been shared with the employees on WhatsApp.",
        "footer": "Automated payroll notification",
        "samples": ["Sunrise Engineering Works", "September 2026", "12", "Rs. 174747"],
    },
}

DEFAULT_WHATSAPP = {
    "enabled": False,
    "provider": "bhashsms",
    "user": "",
    "password": "",
    "sender": "BUZWAP",
    # Placeholders: {user} {pass} {sender} {phone} {template} {params}
    "url_template": "https://bhashsms.com/api/sendmsgutil.php?user={user}&pass={pass}&sender={sender}"
                    "&phone={phone}&text={template}&priority=wa&stype=normal&Params={params}",
    # appended when a document / image is sent: {media_type} {media_url}
    "media_suffix": "&htype={media_type}&url={media_url}",
    "owner_numbers": "",
    "daily_summary_time": "11:00",
    "absent_alert_time": "11:30",
    "realtime_window_minutes": 15,
    "events": {k: {"enabled": False, "template": v["template"], "params": v["params"]} for k, v in WHATSAPP_EVENTS.items()},
}


def company_settings(company):
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s.update((company or {}).get("settings") or {})
    return s


def whatsapp_settings(company):
    w = copy.deepcopy(DEFAULT_WHATSAPP)
    saved = (company or {}).get("whatsapp") or {}
    events = w["events"]
    for k, v in saved.items():
        if k != "events":
            w[k] = v
    for k, v in (saved.get("events") or {}).items():
        events.setdefault(k, {}).update(v or {})
    w["events"] = events
    return w


DEFAULT_SHIFT = {
    "name": "General (9 to 6)", "start": "09:00", "end": "18:00", "grace_minutes": 10,
    "early_grace_minutes": 10, "full_day_hours": 7.0, "half_day_hours": 4.0, "break_minutes": 0,
    "ot_after_minutes": 30, "weekly_off": [6], "is_default": True,
}
