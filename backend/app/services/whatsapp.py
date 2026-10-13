"""WhatsApp sending through BhashSMS (or any HTTP-GET WhatsApp panel) using approved templates."""
import urllib.parse
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


async def send_template(company, phone, template, params, *, event="manual", employee_id=None,
                        media_url=None, media_type="document", w=None):
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
        except Exception as e:
            log.update(status="failed", response=f"{e.__class__.__name__}: {e}")
    await db.whatsapp_logs.insert_one(dict(log))
    log.pop("_id", None)
    return log


async def send_event(company, event, phone, values, *, employee_id=None, media_url=None, force=False):
    w = whatsapp_settings(company)
    ev = w["events"].get(event) or {}
    if not ev.get("enabled") and not force:
        return None
    params = render_params(ev.get("params"), values)
    return await send_template(company, phone, ev.get("template"), params, event=event, employee_id=employee_id,
                               media_url=media_url, w=w)


def owner_numbers(company):
    w = whatsapp_settings(company)
    return [clean_phone(x) for x in str(w.get("owner_numbers") or "").replace(";", ",").split(",") if clean_phone(x)]
