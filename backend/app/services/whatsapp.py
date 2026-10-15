"""WhatsApp sending through BhashSMS (or any HTTP-GET WhatsApp panel) using approved templates."""
import urllib.parse
import datetime
import httpx
from ..db import db
from ..defaults import whatsapp_settings
from ..utils import new_id, utcnow_iso, clean_phone


def _safe(v):
    # BhashSMS separates template params with commas, so commas inside a value must go.
    return str(v if v is not None else "").replace(",", " ").replace("\n", " ").strip() or "-"


def render_params(pattern, values):
    out = []
    for part in (pattern or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(_safe(part.format(**values)))
        except Exception:
            out.append(_safe(part))
    return ",".join(out)


def build_url(w, phone, template, params, media_url=None, media_type="document"):
    q = urllib.parse.quote
    url = w["url_template"].format(user=q(w.get("user", "")), **{"pass": q(w.get("password", ""))},
                                   sender=q(w.get("sender", "")), phone=q(phone), template=q(template),
                                   params=q(params, safe=","))
    if media_url:
        url += (w.get("media_suffix") or "").format(media_type=q(media_type), media_url=q(media_url, safe=""))
    return url


NETWORK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                  httpx.NetworkError, httpx.RemoteProtocolError)
MAX_TRIES = 40          # about 2 days of retries (every few minutes, slowing down)
KEEP_QUEUED_DAYS = 3


async def send_template(company, phone, template, params, *, event="manual", employee_id=None,
                        media_url=None, media_type="document", w=None, retry=None):
    w = w or whatsapp_settings(company)
    phone = clean_phone(phone)
    log = {"id": new_id(), "company_id": company["id"], "event": event, "phone": phone, "template": template,
           "params": params, "employee_id": employee_id, "media_url": media_url, "created_at": utcnow_iso()}
    if not w.get("enabled"):
        log.update(status="skipped", response="WhatsApp is turned off in settings")
    elif len(phone) != 10:
        log.update(status="failed", response="Invalid mobile number")
    elif not template:
        log.update(status="failed", response="No template name set for this message")
    else:
        url = build_url(w, phone, template, params, media_url, media_type)
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.get(url)
            text = r.text.strip()[:300]
            ok = r.status_code == 200 and not any(x in text.lower() for x in ("error", "invalid", "fail", "insufficient"))
            log.update(status="sent" if ok else "failed", response=text or f"HTTP {r.status_code}")
        except NETWORK_ERRORS as e:
            # no internet right now: keep it in the outbox, the scheduler sends it when the internet is back
            log.update(status="queued", attempts=1, next_try_at=_next_try(1), retry=retry,
                       response=f"No internet — will send automatically ({e.__class__.__name__})")
        except Exception as e:
            log.update(status="failed", response=f"{e.__class__.__name__}: {e}")
    await db.whatsapp_logs.insert_one(dict(log))
    log.pop("_id", None)
    return log


async def send_event(company, event, phone, values, *, employee_id=None, media_url=None, force=False, retry=None):
    w = whatsapp_settings(company)
    ev = w["events"].get(event) or {}
    if not ev.get("enabled") and not force:
        return None
    params = render_params(ev.get("params"), values)
    return await send_template(company, phone, ev.get("template"), params, event=event, employee_id=employee_id,
                               media_url=media_url, w=w, retry=retry)


def owner_numbers(company):
    w = whatsapp_settings(company)
    return [clean_phone(x) for x in str(w.get("owner_numbers") or "").replace(";", ",").split(",") if clean_phone(x)]


def _next_try(attempts):
    mins = min(60, 2 * attempts)          # 2, 4, 6 ... up to 60 minutes apart
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=mins)).isoformat()


async def queue(company, event, phone, values, *, employee_id=None, retry=None, reason="No internet"):
    """Put a message in the outbox without trying now (e.g. the salary-slip PDF could not be put online yet)."""
    w = whatsapp_settings(company)
    ev = w["events"].get(event) or {}
    log = {"id": new_id(), "company_id": company["id"], "event": event, "phone": clean_phone(phone),
           "template": ev.get("template"), "params": render_params(ev.get("params"), values), "employee_id": employee_id,
           "media_url": None, "created_at": utcnow_iso(), "status": "queued", "attempts": 0,
           "next_try_at": _next_try(1), "retry": retry, "response": f"{reason} — will send automatically"}
    await db.whatsapp_logs.insert_one(dict(log))
    log.pop("_id", None)
    return log


async def retry_queued(company, limit=30):
    """Send what is waiting in the outbox. Called by the scheduler every minute; cheap when nothing waits."""
    now = datetime.datetime.now(datetime.timezone.utc)
    oldest = (now - datetime.timedelta(days=KEEP_QUEUED_DAYS)).isoformat()
    q = {"company_id": company["id"], "status": "queued", "next_try_at": {"$lte": now.isoformat()}}
    done = 0
    async for lg in db.whatsapp_logs.find(q, {"_id": 0}).sort("created_at", 1).limit(limit):
        attempts = int(lg.get("attempts") or 0) + 1
        if lg["created_at"] < oldest or attempts > MAX_TRIES:
            await db.whatsapp_logs.update_one({"id": lg["id"]}, {"$set": {"status": "failed",
                                              "response": "Not sent: no internet for too long. Send it again from the app."}})
            continue
        params, media = lg.get("params"), lg.get("media_url")
        rt = lg.get("retry") or {}
        try:
            if rt.get("kind") == "payslip":
                from .relay import payslip_link, Offline
                slip = await db.payslips.find_one({"id": rt["slip_id"]}, {"_id": 0})
                if not slip:
                    raise ValueError("salary slip deleted")
                media = await payslip_link(slip, rt.get("base") or "")
                values = dict(rt.get("values") or {}, link=media)
                ev = whatsapp_settings(company)["events"].get("payslip") or {}
                params = render_params(ev.get("params"), values)
        except Exception as e:
            name = e.__class__.__name__
            if name == "Offline":
                await db.whatsapp_logs.update_one({"id": lg["id"]}, {"$set": {"attempts": attempts, "next_try_at": _next_try(attempts)}})
            else:
                await db.whatsapp_logs.update_one({"id": lg["id"]}, {"$set": {"status": "failed", "response": f"{e}"[:300]}})
            continue
        w = whatsapp_settings(company)
        url = build_url(w, lg["phone"], lg.get("template"), params or "", media)
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.get(url)
            text = r.text.strip()[:300]
            ok = r.status_code == 200 and not any(x in text.lower() for x in ("error", "invalid", "fail", "insufficient"))
            upd = {"status": "sent" if ok else "failed", "response": text or f"HTTP {r.status_code}", "attempts": attempts,
                   "params": params, "media_url": media, "sent_at": utcnow_iso()}
            done += 1
        except NETWORK_ERRORS:
            upd = {"attempts": attempts, "next_try_at": _next_try(attempts)}
        except Exception as e:
            upd = {"status": "failed", "response": f"{e.__class__.__name__}: {e}", "attempts": attempts}
        await db.whatsapp_logs.update_one({"id": lg["id"]}, {"$set": upd})
        if rt.get("kind") == "payslip" and upd.get("status"):
            await db.payslips.update_one({"id": rt["slip_id"]}, {"$set": {"whatsapp_status": upd["status"], "whatsapp_at": utcnow_iso()}})
    return done
