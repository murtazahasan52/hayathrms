"""Relay on the cloud site: office/offline installs upload salary-slip PDFs here so WhatsApp can download them."""
import base64
import datetime
from fastapi import APIRouter, HTTPException, Body, Header, Request
from fastapi.responses import Response
from ..db import db
from ..utils import token, utcnow_iso
from ..services import license as L
from .devices import server_base

router = APIRouter(prefix="/api")
MAX_BYTES = 3 * 1024 * 1024
KEEP_DAYS = 30


@router.post("/relay/upload")
async def relay_upload(request: Request, data: dict = Body(...), x_hayat_key: str = Header("")):
    p, err = L.parse_key(x_hayat_key)          # signed by Hayat Enterprises — cannot be faked
    if not p:
        raise HTTPException(401, err or "Licence key required")
    try:
        if datetime.date.fromisoformat(p["e"]) < datetime.date.today():
            raise HTTPException(403, "Licence expired — renew to send salary slips on WhatsApp")
    except ValueError:
        raise HTTPException(401, "Licence key has no valid date")
    try:
        raw = base64.b64decode(data.get("data") or "")
    except Exception:
        raise HTTPException(400, "Bad file")
    if not raw.startswith(b"%PDF") or len(raw) > MAX_BYTES:
        raise HTTPException(400, "Only PDF files up to 3 MB")
    tok = token(24)
    now = datetime.datetime.now(datetime.timezone.utc)
    await db.relay_files.insert_one({"token": tok, "data": base64.b64encode(raw).decode(), "name": str(data.get("name") or "file.pdf")[:80],
                                     "code": p.get("c"), "created_at": utcnow_iso(),
                                     "expires_at": (now + datetime.timedelta(days=KEEP_DAYS)).isoformat()})
    # tidy up old files now and then
    await db.relay_files.delete_many({"expires_at": {"$lt": now.isoformat()}})
    return {"url": f"{server_base(request)}/api/relay/f/{tok}.pdf", "expires_days": KEEP_DAYS}


@router.get("/relay/f/{tok}")
async def relay_file(tok: str):
    f = await db.relay_files.find_one({"token": tok.replace(".pdf", "")}, {"_id": 0})
    if not f or f["expires_at"] < datetime.datetime.now(datetime.timezone.utc).isoformat():
        raise HTTPException(404, "Link expired")
    name = f.get("name") or "file.pdf"
    return Response(base64.b64decode(f["data"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})
