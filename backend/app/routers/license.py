"""Licence: activation on the customer's computer + key making on the issuer (your main server)."""
import random
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from ..db import db
from ..auth import superadmin
from ..utils import new_id, utcnow_iso
from ..services import license as L
from .devices import server_base

router = APIRouter(prefix="/api")
ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------------- this computer ----------------
@router.get("/license/status")
async def lic_status():
    return await L.status(force=True)


@router.post("/license/activate")
async def lic_activate(data: dict = Body(...)):
    ok, info = await L.save_key(data.get("key", ""))
    if not ok:
        raise HTTPException(400, info)
    return {"ok": True, "license": info, "status": await L.status(force=True)}


@router.post("/license/fetch")
async def lic_fetch(data: dict = Body(default={})):
    """Activate / renew online with the licence code (no need to type the long key)."""
    ok, info = await L.fetch_online(data.get("server"), data.get("code"))
    if not ok:
        raise HTTPException(400, info)
    return {"ok": True, "license": info, "status": await L.status(force=True)}


@router.post("/license/issuer-key")
async def lic_issuer_key(data: dict = Body(...), _=Depends(superadmin)):
    """Hayat Enterprises only: paste LICENSE_PRIVATE_KEY after login to make this PC/server the licence maker."""
    ok, msg = L.set_issuer_key(data.get("key", ""))
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "status": await L.status(force=True)}


@router.delete("/license/issuer-key")
async def lic_issuer_key_remove(_=Depends(superadmin)):
    L.remove_issuer_key()
    return {"ok": True}


@router.delete("/license")
async def lic_remove(_=Depends(superadmin)):
    await db.platform.delete_one({"id": "license"})
    L.clear_cache()
    return {"ok": True}


# ---------------- issuer (your main server) ----------------
def _issuer():
    if not L.IS_ISSUER:
        raise HTTPException(400, "This server cannot make licence keys. Set LICENSE_PRIVATE_KEY on your main server.")


def _code():
    return "HL-" + "".join(random.choice(ALPHA) for _ in range(4)) + "-" + "".join(random.choice(ALPHA) for _ in range(4))


def _payload(lic, base):
    return {"v": 1, "c": lic["code"], "n": lic["customer"], "m": int(lic.get("machines") or 0),
            "s": int(lic.get("shops") or 0), "e": lic["valid_till"], "t": datetime.date.today().isoformat(),
            "h": (lic.get("install_id") or "") if lic.get("lock") else "", "u": base}


def _out(lic, request):
    lic = dict(lic)
    lic.pop("_id", None)
    lic["key"] = L.make_key(_payload(lic, server_base(request))) if lic.get("status") != "revoked" else ""
    lic["days_left"] = (datetime.date.fromisoformat(lic["valid_till"]) - datetime.date.today()).days
    return lic


def _add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def _clean(data, cur=None):
    out = {}
    for k in ("customer", "phone", "city", "note", "install_id"):
        if k in data:
            out[k] = str(data[k] or "").strip()
    if "install_id" in out:
        out["install_id"] = out["install_id"].upper()
    for k in ("machines", "shops"):
        if k in data:
            out[k] = max(0, int(data[k] or 0))
    if "lock" in data:
        out["lock"] = bool(data["lock"])
    if "status" in data and data["status"] in ("active", "revoked"):
        out["status"] = data["status"]
    if data.get("valid_till"):
        datetime.date.fromisoformat(data["valid_till"])
        out["valid_till"] = data["valid_till"]
    elif data.get("months"):
        out["valid_till"] = _add_months(datetime.date.today(), int(data["months"])).isoformat()
    return out


@router.get("/admin/licenses")
async def lic_list(request: Request, _=Depends(superadmin)):
    rows = await db.licenses.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    return {"issuer": L.IS_ISSUER, "server": server_base(request),
            "rows": [_out(r, request) if L.IS_ISSUER else r for r in rows]}


@router.post("/admin/licenses")
async def lic_create(request: Request, data: dict = Body(...), user=Depends(superadmin)):
    _issuer()
    lic = {"machines": 1, "shops": 1, "lock": False, "install_id": "", "phone": "", "city": "", "note": "",
           "months": 12, **data}
    lic = _clean(lic)
    if not lic.get("customer"):
        raise HTTPException(400, "Customer / shop name is required")
    if not lic.get("valid_till"):
        lic["valid_till"] = _add_months(datetime.date.today(), 12).isoformat()
    code = _code()
    while await db.licenses.find_one({"code": code}):
        code = _code()
    lic.update(id=new_id(), code=code, status="active", created_at=utcnow_iso(), created_by=user["name"],
               history=[{"at": utcnow_iso(), "what": f"Created · {lic['machines']} machine(s) · till {lic['valid_till']}"}])
    await db.licenses.insert_one(dict(lic))
    return _out(lic, request)


@router.put("/admin/licenses/{lid}")
async def lic_edit(lid: str, request: Request, data: dict = Body(...), _=Depends(superadmin)):
    _issuer()
    cur = await db.licenses.find_one({"id": lid}, {"_id": 0})
    if not cur:
        raise HTTPException(404, "Licence not found")
    upd = _clean(data, cur)
    changes = ", ".join(f"{k} → {v}" for k, v in upd.items() if cur.get(k) != v and k != "note")
    await db.licenses.update_one({"id": lid}, {"$set": upd, **({"$push": {"history": {"at": utcnow_iso(), "what": changes}}}
                                                               if changes else {})})
    return _out(await db.licenses.find_one({"id": lid}, {"_id": 0}), request)


@router.post("/admin/licenses/{lid}/renew")
async def lic_renew(lid: str, request: Request, data: dict = Body(...), _=Depends(superadmin)):
    _issuer()
    cur = await db.licenses.find_one({"id": lid}, {"_id": 0})
    if not cur:
        raise HTTPException(404, "Licence not found")
    months = int(data.get("months") or 12)
    start = max(datetime.date.today(), datetime.date.fromisoformat(cur["valid_till"]))
    till = _add_months(start, months).isoformat()
    await db.licenses.update_one({"id": lid}, {"$set": {"valid_till": till, "status": "active"},
                                               "$push": {"history": {"at": utcnow_iso(), "what": f"Renewed {months} month(s) → {till}"}}})
    if cur.get("company_id"):
        await db.companies.update_one({"id": cur["company_id"]}, {"$set": {"valid_till": till}})
    return _out(await db.licenses.find_one({"id": lid}, {"_id": 0}), request)


@router.delete("/admin/licenses/{lid}")
async def lic_delete(lid: str, _=Depends(superadmin)):
    await db.licenses.delete_one({"id": lid})
    return {"ok": True}


# ---------------- licence tied to a shop (made from the Shops page) ----------------
@router.get("/admin/companies/{cid}/license")
async def shop_license(cid: str, request: Request, _=Depends(superadmin)):
    lic = await db.licenses.find_one({"company_id": cid}, {"_id": 0})
    return {"issuer": L.IS_ISSUER, "license": _out(lic, request) if lic and L.IS_ISSUER else lic}


@router.post("/admin/companies/{cid}/license")
async def shop_license_make(cid: str, request: Request, data: dict = Body(default={}), user=Depends(superadmin)):
    """Create the shop's licence (first time) or renew / update it. Validity and machines follow the shop."""
    _issuer()
    c = await db.companies.find_one({"id": cid}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Shop not found")
    lic = await db.licenses.find_one({"company_id": cid}, {"_id": 0})
    today = datetime.date.today()
    if data.get("valid_till"):
        till = datetime.date.fromisoformat(data["valid_till"]).isoformat()
    elif data.get("months"):
        base = today
        if lic and lic.get("valid_till"):
            base = max(today, datetime.date.fromisoformat(lic["valid_till"]))
        till = _add_months(base, int(data["months"])).isoformat()
    elif c.get("valid_till"):
        till = c["valid_till"]
    else:
        till = _add_months(today, 12).isoformat()
    machines = int(data.get("machines") or c.get("max_devices") or 1)
    upd = {"customer": c["name"], "phone": c.get("phone", ""), "city": c.get("address", ""), "machines": machines,
           "valid_till": till, "status": "active"}
    if "lock" in data:
        upd["lock"] = bool(data["lock"])
    if lic:
        await db.licenses.update_one({"id": lic["id"]}, {"$set": upd, "$push": {"history": {
            "at": utcnow_iso(), "what": f"Renewed/updated from Shops · {machines} machine(s) · till {till}"}}})
    else:
        code = _code()
        while await db.licenses.find_one({"code": code}):
            code = _code()
        lic = {"id": new_id(), "code": code, "company_id": cid, "shops": 1, "lock": bool(data.get("lock", True)),
               "install_id": "", "note": f"Shop: {c['name']}", "created_at": utcnow_iso(), "created_by": user["name"],
               "history": [{"at": utcnow_iso(), "what": f"Created with shop · {machines} machine(s) · till {till}"}], **upd}
        await db.licenses.insert_one(dict(lic))
    # the shop's own validity / machine limit on this server follow the licence
    await db.companies.update_one({"id": cid}, {"$set": {"valid_till": till, "max_devices": machines}})
    return _out(await db.licenses.find_one({"company_id": cid}, {"_id": 0}), request)


async def sync_from_shop(cid, valid_till=None, machines=None):
    """Shop edited on the Shops page -> keep its licence in step."""
    upd = {}
    if valid_till:
        upd["valid_till"] = valid_till
    if machines:
        upd["machines"] = int(machines)
    if upd:
        await db.licenses.update_one({"company_id": cid}, {"$set": upd})


@router.post("/license/online")
async def lic_online(request: Request, data: dict = Body(...)):
    """Called by customers' computers (activation with the code, and the daily renewal check)."""
    if not L.IS_ISSUER:
        raise HTTPException(400, "This server does not issue licences")
    code = str(data.get("code") or "").strip().upper()
    iid = str(data.get("install_id") or "").strip().upper()
    lic = await db.licenses.find_one({"code": code}, {"_id": 0})
    if not lic:
        raise HTTPException(404, "Licence code not found")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    seen = {"last_seen": utcnow_iso(), "last_ip": ip, "last_install_id": iid}
    if lic.get("status") == "revoked":
        await db.licenses.update_one({"id": lic["id"]}, {"$set": seen})
        return JSONResponse({"detail": "This licence was cancelled. Contact Hayat Enterprises.", "revoked": True}, 403)
    if lic.get("lock"):
        if lic.get("install_id") and iid and lic["install_id"] != iid:
            await db.licenses.update_one({"id": lic["id"]}, {"$set": seen})
            raise HTTPException(403, f"This licence is already used on another computer ({lic['install_id']}). "
                                     "Ask Hayat Enterprises to move it.")
        if not lic.get("install_id") and iid:
            seen["install_id"] = iid
            lic["install_id"] = iid
    await db.licenses.update_one({"id": lic["id"]}, {"$set": seen})
    return {"key": L.make_key(_payload(lic, server_base(request)))}
