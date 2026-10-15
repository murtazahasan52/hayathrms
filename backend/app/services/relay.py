"""Public links for salary-slip PDFs from offline / office installs.

BhashSMS (WhatsApp) must DOWNLOAD the PDF from the internet. A PC install's own address (localhost, 192.168.x.x)
is not reachable from outside, so the PC uploads the PDF to the Hayat cloud site (the relay) and sends that link.
Only computers with a genuine Hayat licence key (or the licence maker itself) can upload. Files expire in 30 days.
"""
import os
import base64
import ipaddress
import datetime
import urllib.parse
import httpx
from ..db import db
from ..config import PUBLIC_BASE_URL
from . import license as L

RELAY_URL = (os.environ.get("WHATSAPP_RELAY_URL") or "https://hayathrms.vercel.app").rstrip("/")
NETWORK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                  httpx.RemoteProtocolError, httpx.NetworkError)


class Offline(Exception):
    """No internet right now — try again later."""


class RelayError(Exception):
    pass


def is_public(base):
    """True when this server's address can be reached from the internet (so no relay is needed)."""
    try:
        host = urllib.parse.urlparse(base).hostname or ""
    except Exception:
        return False
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".localhost") or host.endswith(".lan"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return "." in host  # a domain name


async def relay_key():
    """What proves to the relay that this is a genuine Hayat install."""
    if L.IS_ISSUER:
        until = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
        return L.make_key({"v": 1, "c": "RELAY", "n": "Licence maker", "m": 0, "s": 0, "e": until,
                           "t": datetime.date.today().isoformat(), "h": "", "u": ""})
    doc = await db.platform.find_one({"id": "license"}, {"_id": 0, "key": 1}) or {}
    return doc.get("key")


async def relay_server():
    doc = await db.platform.find_one({"id": "license"}, {"_id": 0, "key": 1, "server": 1}) or {}
    p, _ = L.parse_key(doc["key"]) if doc.get("key") else (None, None)
    return ((p or {}).get("u") or doc.get("server") or os.environ.get("LICENSE_SERVER") or RELAY_URL).rstrip("/")


async def upload_pdf(pdf: bytes, name: str) -> str:
    key = await relay_key()
    if not key:
        raise RelayError("This computer has no licence key, so salary slip PDFs cannot be put online for WhatsApp.")
    server = await relay_server()
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(server + "/api/relay/upload", headers={"X-Hayat-Key": key},
                             json={"name": name, "data": base64.b64encode(pdf).decode()})
    except NETWORK_ERRORS as e:
        raise Offline(f"No internet ({e.__class__.__name__})")
    if r.status_code != 200:
        try:
            msg = r.json().get("detail")
        except Exception:
            msg = r.text[:150]
        raise RelayError(f"PDF link server refused: {msg}")
    return r.json()["url"]


async def payslip_link(slip, base):
    """Public link to the salary slip PDF: this server's own link when it is on the internet, else via the relay."""
    base = (PUBLIC_BASE_URL or base or "").rstrip("/")
    if is_public(base):
        return f"{base}/api/public/payslip/{slip['token']}.pdf"
    from ..routers.work import _pdf_for
    pdf, _, emp = await _pdf_for(slip)
    return await upload_pdf(pdf, f"Salary_Slip_{slip['month']}_{slip.get('emp_code') or ''}.pdf")
