"""Background loop: direct device pulls, device offline marking, daily WhatsApp summary & absent alerts."""
import asyncio
import datetime
import traceback
from ..db import db
from ..defaults import whatsapp_settings
from ..utils import now_local, utcnow_iso, hm
from .attendance import process_range
from .device_sync import pull_device
from . import whatsapp


async def _company_tick(company):
    now = now_local(company)
    today = now.date().isoformat()

    # 1. direct-pull devices
    async for dev in db.devices.find({"company_id": company["id"], "mode": "direct", "enabled": {"$ne": False}}, {"_id": 0}):
        interval = int(dev.get("pull_interval_minutes") or 5)
        last = dev.get("last_sync")
        due = True
        if last:
            try:
                due = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last)).total_seconds() >= interval * 60
            except Exception:
                pass
        if due:
            await pull_device(company, dev)

    # 2. offline marking (no contact for 15 minutes)
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)).isoformat()
    await db.devices.update_many({"company_id": company["id"], "status": "online", "last_seen": {"$lt": cutoff}},
                                 {"$set": {"status": "offline"}})

    # 3. WhatsApp scheduled messages
    w = whatsapp_settings(company)
    if not w.get("enabled"):
        return
    # 3a. outbox: messages that waited for the internet
    try:
        from .whatsapp import retry_queued
        await retry_queued(company)
    except Exception:
        traceback.print_exc()
    mins = now.hour * 60 + now.minute
    flags = company.get("sched_flags") or {}
    for key, time_key, fn in (("daily_summary", "daily_summary_time", send_daily_summary),
                              ("absent_alert", "absent_alert_time", send_absent_alerts)):
        if not w["events"].get(key, {}).get("enabled"):
            continue
        if flags.get(key) == today:
            continue
        if mins >= hm(w.get(time_key) or "11:00"):
            await db.companies.update_one({"id": company["id"]}, {"$set": {f"sched_flags.{key}": today}})
            await fn(company, now.date())


async def day_report(company, day):
    await process_range(company, day, day)
    rows = await db.attendance.find({"company_id": company["id"], "date": day.isoformat()}, {"_id": 0}).to_list(10000)
    present = [r for r in rows if r["status"] in ("P", "HD", "MIS", "IN")]
    absent = [r for r in rows if r["status"] in ("A", "-")]
    late = [r for r in present if (r.get("late_minutes") or 0) > 0]
    return rows, present, absent, late


async def send_daily_summary(company, day):
    rows, present, absent, late = await day_report(company, day)
    values = {"date": day.strftime("%d-%m-%Y"), "total": len(rows), "present": len(present), "absent": len(absent),
              "late": len(late), "absent_names": " | ".join(r.get("employee_name", "") for r in absent[:25]) or "None",
              "company": company["name"]}
    out = []
    for ph in whatsapp.owner_numbers(company):
        out.append(await whatsapp.send_event(company, "daily_summary", ph, values))
    return out


async def send_absent_alerts(company, day):
    rows, present, absent, late = await day_report(company, day)
    out = []
    for r in absent:
        emp = await db.employees.find_one({"id": r["employee_id"]}, {"_id": 0})
        if emp and emp.get("phone") and emp.get("whatsapp_alerts") is not False:
            out.append(await whatsapp.send_event(company, "absent_alert", emp["phone"],
                                                 {"name": emp["name"], "date": day.strftime("%d-%m-%Y"),
                                                  "company": company["name"]}, employee_id=emp["id"]))
    return out


async def tick_all():
    n = 0
    async for company in db.companies.find({"active": {"$ne": False}}, {"_id": 0}):
        try:
            await _company_tick(company)
            n += 1
        except Exception:
            traceback.print_exc()
    return n


async def run_scheduler():
    await asyncio.sleep(5)
    while True:
        try:
            async for company in db.companies.find({"active": {"$ne": False}}, {"_id": 0}):
                try:
                    await _company_tick(company)
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()
        try:
            from .license import auto_refresh
            await auto_refresh()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(60)
