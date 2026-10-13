"""Biometric devices: CRUD, eSSL/ZKTeco ADMS push (iclock), Hikvision HTTP push, LAN agent API, direct pull."""
import json
import asyncio
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, Request, Header
from fastapi.responses import PlainTextResponse
from ..db import db
from ..auth import company_ctx, Ctx
from ..config import PUBLIC_BASE_URL
from ..utils import new_id, token, utcnow_iso, now_local
from ..services.ingest import ingest
from ..services.device_sync import pull_device, save_device_users
from ..services.sync import purge_punches
from ..services import zkclient, hikclient
from ..services.photos import save_photo, decode_any, codes_with_photos

router = APIRouter(prefix="/api")

DEV_FIELDS = ("name", "brand", "mode", "ip", "port", "comm_key", "username", "password", "serial", "location",
              "enabled", "pull_interval_minutes", "initial_days")


def live_status(dev):
    """'online' only if the device was heard from in the last 15 minutes."""
    if dev.get("status") == "online" and dev.get("last_seen"):
        try:
            age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(dev["last_seen"])
            if age.total_seconds() > 900:
                return "offline"
        except Exception:
            pass
    return dev.get("status")


def _public(dev):
    d = dict(dev)
    d["status"] = live_status(dev)
    d["has_password"] = bool(d.pop("password", None))
    d.pop("adms_commands", None)
    return d


def server_base(request: Request):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}"


@router.get("/devices")
async def list_devices(request: Request, ctx: Ctx = Depends(company_ctx)):
    rows = await db.devices.find({"company_id": ctx.cid}, {"_id": 0}).to_list(200)
    return {"devices": [_public(d) for d in rows], "server_base": server_base(request),
            "agent_key": ctx.company.get("agent_key") if ctx.role in ("owner", "superadmin") else None}


@router.post("/devices")
async def add_device(data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    n = await db.devices.count_documents({"company_id": ctx.cid})
    from ..services.license import limit_check
    await limit_check("machines")
    from ..services.license import license_required
    if not license_required() and n >= int(ctx.company.get("max_devices") or 10**6):
        raise HTTPException(403, f"Device limit ({ctx.company['max_devices']}) reached for your plan")
    d = {k: data.get(k) for k in DEV_FIELDS if k in data}
    d.setdefault("brand", "essl")
    d.setdefault("mode", "agent")
    if d["mode"] == "adms":
        if not d.get("serial"):
            raise HTTPException(400, "Serial number (SN) is required for ADMS push mode")
        d["serial"] = d["serial"].strip().upper()
        if await db.devices.find_one({"serial": d["serial"]}):
            raise HTTPException(400, "A device with this serial number is already registered")
    if d["mode"] in ("direct", "agent") and not d.get("ip"):
        raise HTTPException(400, "IP address is required")
    d.update(id=new_id(), company_id=ctx.cid, push_token=token(18), status="new", enabled=d.get("enabled", True),
             total_punches=0, created_at=utcnow_iso())
    if not d.get("port"):
        d["port"] = 80 if d["brand"] == "hikvision" else 4370
    await db.devices.insert_one(dict(d))
    await db.unknown_devices.delete_many({"serial": d.get("serial")})
    return _public(d)


@router.put("/devices/{did}")
async def edit_device(did: str, data: dict = Body(...), ctx: Ctx = Depends(company_ctx)):
    d = {k: data[k] for k in DEV_FIELDS if k in data}
    if not d.get("password"):
        d.pop("password", None)
    if d.get("serial"):
        d["serial"] = d["serial"].strip().upper()
    await db.devices.update_one({"id": did, "company_id": ctx.cid}, {"$set": d})
    return {"ok": True}


@router.delete("/devices/{did}")
async def del_device(did: str, ctx: Ctx = Depends(company_ctx), punches: int = 0):
    n = 0
    if punches:
        if ctx.role not in ("owner", "superadmin"):
            raise HTTPException(403, "Only the owner can delete punch logs")
        n = await purge_punches(ctx.company, {"device_id": did})
    await db.devices.delete_one({"id": did, "company_id": ctx.cid})
    await db.device_users.delete_many({"company_id": ctx.cid, "device_id": did})
    return {"ok": True, "punches_deleted": n}


def _zk_update_names(dev, wanted):
    with zkclient.ZKClient(dev["ip"], dev.get("port") or 4370, dev.get("comm_key") or 0, timeout=20) as z:
        return z.update_names(wanted)


def _hik_update_names(dev, wanted):
    h = hikclient.HikClient(dev["ip"], dev.get("port") or 80, dev.get("username") or "admin", dev.get("password") or "")
    return h.update_names(wanted)


def _adms_escape(v):
    return str(v).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _my_lan_prefixes():
    import socket
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        out.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        out += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except Exception:
        pass
    pref = []
    for ip in out:
        if ip.startswith(("192.168.", "10.", "172.")):
            p = ip.rsplit(".", 1)[0]
            if p not in pref:
                pref.append(p)
    return pref


@router.post("/devices/discover")
async def discover_devices(data: dict = Body(default={}), ctx: Ctx = Depends(company_ctx)):
    """Scan the office network for eSSL / ZKTeco machines (works when the app runs on a PC in that office)."""
    import os
    if os.environ.get("VERCEL") and not data.get("prefix"):
        return {"found": [], "scanned": [], "message": "The online app can't see your office network. Run find_devices.bat from the agent folder on the shop PC to list the machines and their IPs."}
    prefixes = [data["prefix"]] if data.get("prefix") else _my_lan_prefixes()
    if not prefixes:
        return {"found": [], "scanned": [], "message": "This server is not on an office network. Use the LAN agent's find_devices.bat at the site instead."}
    found = []
    for p in prefixes[:3]:
        found += await asyncio.to_thread(zkclient.discover, p, int(data.get("port") or 4370), int(data.get("comm_key") or 0))
    known = {d.get("hw_serial"): d["name"] async for d in db.devices.find({"company_id": ctx.cid, "hw_serial": {"$exists": True}}, {"hw_serial": 1, "name": 1})}
    for f in found:
        f["added_as"] = known.get(f.get("serial")) if f.get("serial") else None
    return {"found": found, "scanned": [p + ".1-254" for p in prefixes[:3]]}


@router.post("/devices/push-names")
async def push_names(data: dict = Body(default={}), ctx: Ctx = Depends(company_ctx)):
    """Write employee names from the app onto the machines. {employee_ids?, device_ids?}"""
    q = {"company_id": ctx.cid, "active": {"$ne": False}}
    if data.get("employee_ids"):
        q["id"] = {"$in": data["employee_ids"]}
    wanted = {}
    async for e in db.employees.find(q, {"_id": 0, "code": 1, "extra_codes": 1, "name": 1}):
        for c in [e["code"]] + list(e.get("extra_codes") or []):
            if c and e.get("name"):
                wanted[str(c)] = e["name"].strip()
    if not wanted:
        raise HTTPException(400, "No employees with a name to send")
    dq = {"company_id": ctx.cid, "enabled": {"$ne": False}}
    if data.get("device_ids"):
        dq["id"] = {"$in": data["device_ids"]}
    results = []
    async for d in db.devices.find(dq, {"_id": 0}):
        r = {"device": d["name"], "mode": d["mode"]}
        try:
            if d["mode"] == "direct":
                fn = _hik_update_names if d["brand"] == "hikvision" else _zk_update_names
                up, miss, same = await asyncio.to_thread(fn, d, wanted)
                r.update(ok=True, message=f"{len(up)} renamed, {len(same)} already correct"
                         + (f", {len(miss)} not enrolled on this machine" if miss else ""))
            elif d["mode"] == "agent":
                await db.devices.update_one({"id": d["id"]}, {"$set": {"pending.push_names": wanted}})
                r.update(ok=True, message="Sent to the LAN agent — names are written within a minute")
            elif d["mode"] == "adms":
                base = int(datetime.datetime.now().timestamp()) % 90000
                cmds = [{"id": base + i, "cmd": f"DATA UPDATE USERINFO PIN={_adms_escape(c)}\tName={_adms_escape(n)}"}
                        for i, (c, n) in enumerate(wanted.items())]
                await db.devices.update_one({"id": d["id"]}, {"$push": {"adms_commands": {"$each": cmds}}})
                r.update(ok=True, message=f"{len(cmds)} names queued — the machine applies them on its next check-in")
            else:
                r.update(ok=False, message="Hikvision push mode can't write to the terminal. Use LAN agent or static IP mode.")
        except Exception as e:
            r.update(ok=False, message=str(e))
        results.append(r)
    if not results:
        return {"results": [], "message": "No devices added yet"}
    return {"results": results, "employees": len(wanted)}


@router.post("/sync/all")
async def sync_all(data: dict = Body(default={}), ctx: Ctx = Depends(company_ctx)):
    """One button: fetch punch logs and user lists from every device, optionally add new machine users as employees."""
    days = int(data.get("days_back") or 0) or None
    want_users = data.get("users", True)
    want_photos = bool(data.get("photos"))
    if "auto_add" in data:
        await db.companies.update_one({"id": ctx.cid}, {"$set": {"settings.auto_add_employees": bool(data["auto_add"])}})
        ctx.company.setdefault("settings", {})["auto_add_employees"] = bool(data["auto_add"])
    now = now_local(ctx.company)
    results = []
    before = await db.employees.count_documents({"company_id": ctx.cid})
    async for d in db.devices.find({"company_id": ctx.cid, "enabled": {"$ne": False}}, {"_id": 0}):
        r = {"device": d["name"], "mode": d["mode"]}
        if d["mode"] == "direct":
            res = await pull_device(ctx.company, d, days_back=days, photos="all" if want_photos else False)
            r.update(ok=res["ok"], message=(f"{res['new_punches']} new punches, {res['users']} users on machine"
                                            + (f", {res['photos']} photos" if res.get("photos") else "")
                                            if res["ok"] else res["error"]))
        elif d["mode"] == "agent":
            await db.devices.update_one({"id": d["id"]}, {"$set": {"pending.sync": True, "pending.days_back": days or 0,
                                                                   "pending.users": bool(want_users),
                                                                   "pending.photos": want_photos}})
            agent_seen = ctx.company.get("agent_last_seen")
            r.update(ok=True, message="Sent to the LAN agent — it fetches within a minute"
                     + ("" if agent_seen else " (agent has not connected yet)"))
        elif d["mode"] == "adms":
            start = now - datetime.timedelta(days=days or 7)
            cmds = [{"id": int(now.timestamp()) % 100000,
                     "cmd": f"DATA QUERY ATTLOG StartTime={start:%Y-%m-%d} 00:00:00\tEndTime={now:%Y-%m-%d} 23:59:59"}]
            if want_users:
                cmds.append({"id": (int(now.timestamp()) + 1) % 100000, "cmd": "DATA QUERY USERINFO"})
            await db.devices.update_one({"id": d["id"]}, {"$push": {"adms_commands": {"$each": cmds}}})
            r.update(ok=True, message="Queued — the machine uploads on its next check-in (about 30 s)"
                     + (". Photos come with the user list if the machine stores user photos." if want_photos else ""))
        else:
            r.update(ok=True, message="Live push — punches already arrive in real time")
        results.append(r)
    if data.get("auto_add"):
        from ..services.sync import auto_add_employees
        await auto_add_employees(ctx.company, force=True)
    added = await db.employees.count_documents({"company_id": ctx.cid}) - before
    from ..services.attendance import process_range
    first = (now.date().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    await process_range(ctx.company, first, now.date())
    if not results:
        return {"ok": False, "results": [], "employees_added": added, "message": "No devices added yet"}
    return {"ok": True, "results": results, "employees_added": added}


async def _get_dev(ctx, did):
    d = await db.devices.find_one({"id": did, "company_id": ctx.cid}, {"_id": 0})
    if not d:
        raise HTTPException(404, "Device not found")
    return d


@router.post("/devices/{did}/test")
async def test_device(did: str, ctx: Ctx = Depends(company_ctx)):
    d = await _get_dev(ctx, did)
    if d["mode"] != "direct":
        msgs = {"adms": "ADMS devices connect to the server by themselves — check 'Last seen'.",
                "hik_push": "Hikvision push devices connect by themselves — check 'Last seen'.",
                "agent": "This device is read by the LAN agent. Run test_device.bat on the site PC, or check 'Last seen'."}
        return {"ok": d.get("status") == "online", "message": msgs.get(d["mode"], ""), "last_seen": d.get("last_seen")}
    try:
        if d["brand"] == "hikvision":
            info = await asyncio.to_thread(hikclient.test_device, d["ip"], d.get("port") or 80, d.get("username"), d.get("password"))
        else:
            moved = None
            try:
                info = await asyncio.to_thread(zkclient.test_device, d["ip"], d.get("port") or 4370, d.get("comm_key") or 0)
            except zkclient.ZKError as e:
                from ..services.device_sync import relocate
                if "Cannot reach" not in str(e):
                    raise
                old_ip = d["ip"]
                moved = await relocate(d)
                if not moved:
                    raise
                info = await asyncio.to_thread(zkclient.test_device, d["ip"], d.get("port") or 4370, d.get("comm_key") or 0)
                info["moved"] = f"Machine's IP changed from {old_ip} to {moved} — updated automatically"
            upd = {"status": "online", "last_seen": utcnow_iso(), "sync_error": None}
            if info.get("serial"):
                upd["hw_serial"] = info["serial"]
            await db.devices.update_one({"id": did}, {"$set": upd})
            return {"ok": True, "message": info.get("moved") or f"Connected over {info.get('protocol', 'TCP')}", **info}
        await db.devices.update_one({"id": did}, {"$set": {"status": "online", "last_seen": utcnow_iso(), "sync_error": None}})
        return {"ok": True, "message": "Connected", **info}
    except Exception as e:
        await db.devices.update_one({"id": did}, {"$set": {"status": "error", "sync_error": str(e)}})
        return {"ok": False, "message": str(e)}


@router.post("/devices/{did}/sync")
async def sync_device(did: str, data: dict = Body(default={}), ctx: Ctx = Depends(company_ctx)):
    d = await _get_dev(ctx, did)
    days = int(data.get("days_back") or 0) or None
    if d["mode"] == "direct":
        return await pull_device(ctx.company, d, days_back=days)
    if d["mode"] == "agent":
        await db.devices.update_one({"id": did}, {"$set": {"pending.sync": True, "pending.days_back": days or 0}})
        return {"ok": True, "message": "Sync requested — the LAN agent will fetch on its next round (within a minute)."}
    if d["mode"] == "adms":
        now = now_local(ctx.company)
        start = now - datetime.timedelta(days=days or 31)
        cmd = f"DATA QUERY ATTLOG StartTime={start:%Y-%m-%d} 00:00:00\tEndTime={now:%Y-%m-%d} 23:59:59"
        await db.devices.update_one({"id": did}, {"$push": {"adms_commands": {"id": int(now.timestamp()) % 100000, "cmd": cmd}}})
        return {"ok": True, "message": "Re-upload command queued — the machine will send its logs on its next check-in."}
    return {"ok": False, "message": "Hikvision push mode only receives live events. Use LAN agent or direct mode to fetch old logs."}


@router.post("/devices/{did}/fetch-users")
async def fetch_users(did: str, ctx: Ctx = Depends(company_ctx)):
    d = await _get_dev(ctx, did)
    if d["mode"] == "direct":
        try:
            if d["brand"] == "hikvision":
                h = hikclient.HikClient(d["ip"], d.get("port") or 80, d.get("username"), d.get("password"))
                users = await asyncio.to_thread(h.get_users)
            else:
                def _u():
                    with zkclient.ZKClient(d["ip"], d.get("port") or 4370, d.get("comm_key") or 0) as z:
                        return z.get_users()
                users = await asyncio.to_thread(_u)
            await save_device_users(ctx.company, d, users)
            pics = 0
            if d["brand"] == "hikvision":
                from ..services.device_sync import pull_photos
                try:
                    pics = await pull_photos(ctx.company, d, force=True)
                except Exception:
                    pass
            return {"ok": True, "message": f"{len(users)} users read from the machine" + (f", {pics} photos" if pics else ""), "count": len(users)}
        except Exception as e:
            return {"ok": False, "message": str(e)}
    if d["mode"] == "agent":
        await db.devices.update_one({"id": did}, {"$set": {"pending.users": True, "pending.photos": True}})
        return {"ok": True, "message": "The LAN agent will read users on its next round."}
    if d["mode"] == "adms":
        await db.devices.update_one({"id": did}, {"$push": {"adms_commands": {"id": int(datetime.datetime.now().timestamp()) % 100000,
                                                                             "cmd": "DATA QUERY USERINFO"}}})
        return {"ok": True, "message": "User list request queued for the machine."}
    return {"ok": False, "message": "Not available in Hikvision push mode."}


@router.get("/punches")
async def list_punches(ctx: Ctx = Depends(company_ctx), date: str = None, emp_code: str = None, limit: int = 500):
    q = {"company_id": ctx.cid}
    if date:
        q["date"] = date
    if emp_code:
        q["emp_code"] = emp_code
    rows = await db.punches.find(q, {"_id": 0}).sort("ts", -1).to_list(min(limit, 5000))
    names = {}
    async for e in db.employees.find({"company_id": ctx.cid}, {"_id": 0, "code": 1, "name": 1, "extra_codes": 1}):
        for c in [e["code"]] + (e.get("extra_codes") or []):
            names[c] = e["name"]
    for p in rows:
        p["employee_name"] = names.get(p["emp_code"], "— not registered —")
    return rows


# =====================================================================
# eSSL / ZKTeco ADMS ("Cloud Server" / push SDK) — device calls these itself
# =====================================================================
iclock = APIRouter()


def _txt(s):
    return PlainTextResponse(s, media_type="text/plain")


async def _adms_device(sn, request):
    sn = (sn or "").strip().upper()
    dev = await db.devices.find_one({"serial": sn, "mode": "adms"}, {"_id": 0})
    if not dev:
        await db.unknown_devices.update_one({"serial": sn}, {"$set": {"serial": sn, "ip": request.client.host if request.client else "",
                                                                      "last_seen": utcnow_iso(), "kind": "eSSL/ZKTeco ADMS"}},
                                            upsert=True)
        return None, None
    company = await db.companies.find_one({"id": dev["company_id"], "active": {"$ne": False}}, {"_id": 0})
    return dev, company


@iclock.get("/cdata")
async def adms_handshake(request: Request, SN: str = ""):
    dev, company = await _adms_device(SN, request)
    if not dev or not company:
        return _txt("OK")
    await db.devices.update_one({"id": dev["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online",
                                                             "ip_seen": request.client.host if request.client else ""}})
    stamp = dev.get("adms_stamp") or "None"
    return _txt(f"GET OPTION FROM: {SN}\nATTLOGStamp={stamp}\nOPERLOGStamp=9999\nATTPHOTOStamp=None\nErrorDelay=30\n"
                f"Delay=10\nTransTimes=00:00;14:05\nTransInterval=1\nTransFlag=TransData AttLog\tOpLog\tEnrollUser\tChgUser\tUserPic\tBioPhoto\n"
                f"TimeZone=5.5\nRealtime=1\nEncrypt=None\n")


@iclock.post("/cdata")
async def adms_upload(request: Request, SN: str = "", table: str = "", Stamp: str = None):
    dev, company = await _adms_device(SN, request)
    body = (await request.body()).decode("utf-8", errors="ignore")
    if not dev or not company:
        return _txt("OK")
    lines = [l for l in body.replace("\r", "").split("\n") if l.strip()]
    t = (table or "").upper()
    if t == "ATTLOG":
        rows = []
        for l in lines:
            p = l.split("\t")
            if len(p) >= 2:
                rows.append({"user_id": p[0].strip(), "ts": p[1].strip(), "state": p[2] if len(p) > 2 else None,
                             "verify": p[3] if len(p) > 3 else None})
        await ingest(company, dev, rows, "adms")
        upd = {"last_seen": utcnow_iso(), "status": "online"}
        if Stamp:
            upd["adms_stamp"] = Stamp
        await db.devices.update_one({"id": dev["id"]}, {"$set": upd})
        return _txt(f"OK: {len(rows)}")
    if t in ("OPERLOG", "USERINFO", "USER", "USERPIC", "BIOPHOTO", "BIODATA"):
        users, pics = [], 0
        for l in lines:
            head = l.split(" ", 1)[0] if " " in l[:12] else ""
            kv = dict(x.split("=", 1) for x in (l.split(" ", 1)[1] if head else l).split("\t") if "=" in x)
            pin = (kv.get("PIN") or "").strip()
            if not pin:
                continue
            if head in ("USERPIC", "BIOPHOTO") or (t in ("USERPIC", "BIOPHOTO") and kv.get("Content")):
                if kv.get("Content"):
                    try:
                        pics += bool(await save_photo(company, pin, decode_any(kv["Content"]), "essl-adms"))
                    except Exception:
                        pass
            elif head in ("USER", "") and "Name" in kv or head == "USER":
                users.append({"user_id": pin, "name": kv.get("Name", "").strip()})
        if users:
            await save_device_users(company, dev, users)
        await db.devices.update_one({"id": dev["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online"}})
        return _txt(f"OK: {len(lines)}")
    await db.devices.update_one({"id": dev["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online"}})
    return _txt("OK")


@iclock.get("/getrequest")
async def adms_getrequest(request: Request, SN: str = ""):
    dev, company = await _adms_device(SN, request)
    if not dev:
        return _txt("OK")
    await db.devices.update_one({"id": dev["id"]}, {"$set": {"last_seen": utcnow_iso(), "status": "online"},
                                                    "$unset": {"adms_commands": ""}})
    cmds = dev.get("adms_commands") or []
    if not cmds:
        return _txt("OK")
    return _txt("".join(f"C:{c['id']}:{c['cmd']}\n" for c in cmds))


@iclock.post("/devicecmd")
async def adms_devicecmd(request: Request, SN: str = ""):
    await _adms_device(SN, request)
    return _txt("OK")


@iclock.get("/ping")
async def adms_ping(request: Request, SN: str = ""):
    return _txt("OK")


# .aspx aliases used by some eSSL firmware
iclock.add_api_route("/cdata.aspx", adms_handshake, methods=["GET"])
iclock.add_api_route("/cdata.aspx", adms_upload, methods=["POST"])
iclock.add_api_route("/getrequest.aspx", adms_getrequest, methods=["GET"])
iclock.add_api_route("/devicecmd.aspx", adms_devicecmd, methods=["POST"])


# =====================================================================
# Hikvision "HTTP Listening" / Event notification push
# =====================================================================
def _find_event(obj):
    if isinstance(obj, dict):
        if "AccessControllerEvent" in obj:
            return obj, obj["AccessControllerEvent"]
        for v in obj.values():
            r = _find_event(v)
            if r:
                return r
    return None


@router.api_route("/push/hikvision/{tok}", methods=["GET", "POST", "PUT"])
async def hik_push(tok: str, request: Request):
    dev = await db.devices.find_one({"push_token": tok}, {"_id": 0})
    if not dev:
        return _txt("OK")
    company = await db.companies.find_one({"id": dev["company_id"], "active": {"$ne": False}}, {"_id": 0})
    if not company:
        return _txt("OK")
    payloads = []
    ctype = request.headers.get("content-type", "")
    try:
        if "multipart" in ctype or "form" in ctype:
            form = await request.form()
            for _, v in form.multi_items():
                raw = await v.read() if hasattr(v, "read") else v
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    payloads.append(json.loads(raw))
                except Exception:
                    pass
        else:
            body = (await request.body()).decode("utf-8", errors="ignore").strip()
            if body:
                payloads.append(json.loads(body))
    except Exception:
        pass
    rows = []
    for p in payloads:
        found = _find_event(p)
        if not found:
            continue
        outer, ev = found
        emp = ev.get("employeeNoString") or (str(ev.get("employeeNo")) if ev.get("employeeNo") else "")
        if not emp:
            continue
        rows.append({"user_id": emp, "ts": outer.get("dateTime") or ev.get("time"), "name": ev.get("name"),
                     "attendance_status": ev.get("attendanceStatus"), "verify": ev.get("currentVerifyMode")})
    await ingest(company, dev, [r for r in rows if r["ts"]], "hik_push")
    return _txt("OK")


# =====================================================================
# LAN Agent API (Hayat Sync Agent running on a PC at the site)
# =====================================================================
async def agent_company(x_agent_key: str = Header(None)):
    if not x_agent_key:
        raise HTTPException(401, "Agent key missing")
    c = await db.companies.find_one({"agent_key": x_agent_key}, {"_id": 0})
    if not c:
        raise HTTPException(401, "Invalid agent key")
    if not c.get("active", True):
        raise HTTPException(403, "Company suspended")
    return c


@router.get("/agent/config")
async def agent_config(company=Depends(agent_company)):
    devs = await db.devices.find({"company_id": company["id"], "mode": "agent", "enabled": {"$ne": False}}, {"_id": 0}).to_list(100)
    out = []
    for d in devs:
        out.append({k: d.get(k) for k in ("id", "name", "brand", "ip", "port", "comm_key", "username", "password",
                                          "last_punch_at", "pending", "initial_days", "hw_serial")})
    await db.companies.update_one({"id": company["id"]}, {"$set": {"agent_last_seen": utcnow_iso()}})
    return {"company": company["name"], "timezone": (company.get("settings") or {}).get("timezone", "Asia/Kolkata"),
            "devices": out, "server_time": utcnow_iso(), "have_photos": await codes_with_photos(company["id"])}


@router.post("/agent/photos")
async def agent_photos(data: dict = Body(...), company=Depends(agent_company)):
    dev = await db.devices.find_one({"id": data.get("device_id"), "company_id": company["id"]}, {"_id": 0})
    if not dev:
        raise HTTPException(404, "Device not found")
    n = 0
    for p in data.get("photos") or []:
        try:
            n += bool(await save_photo(company, p["user_id"], decode_any(p["data"]), dev.get("brand", "")))
        except Exception:
            pass
    if data.get("done"):
        await db.devices.update_one({"id": dev["id"]}, {"$set": {"pending.photos": False}})
    return {"ok": True, "saved": n}


@router.post("/agent/punches")
async def agent_punches(data: dict = Body(...), company=Depends(agent_company)):
    dev = await db.devices.find_one({"id": data.get("device_id"), "company_id": company["id"]}, {"_id": 0})
    if not dev:
        raise HTTPException(404, "Device not found")
    n = await ingest(company, dev, data.get("rows") or [], "agent")
    upd = {"last_sync": utcnow_iso(), "sync_error": None, "status": "online", "last_seen": utcnow_iso()}
    if data.get("done_sync"):
        upd["pending.sync"] = False
        upd["pending.days_back"] = 0
    await db.devices.update_one({"id": dev["id"]}, {"$set": upd})
    return {"ok": True, "new": n}


@router.post("/agent/users")
async def agent_users(data: dict = Body(...), company=Depends(agent_company)):
    dev = await db.devices.find_one({"id": data.get("device_id"), "company_id": company["id"]}, {"_id": 0})
    if not dev:
        raise HTTPException(404, "Device not found")
    users = data.get("users") or []
    await save_device_users(company, dev, users)
    await db.devices.update_one({"id": dev["id"]}, {"$set": {"pending.users": False, "device_users": len(users)}})
    return {"ok": True, "count": len(users)}


@router.post("/agent/status")
async def agent_status(data: dict = Body(...), company=Depends(agent_company)):
    upd = {"last_seen": utcnow_iso()}
    if data.get("ok"):
        upd.update(status="online", sync_error=None)
    else:
        upd.update(status="error", sync_error=str(data.get("error") or "")[:300])
    if data.get("info"):
        upd["device_info"] = data["info"]
        if data["info"].get("serial"):
            upd["hw_serial"] = data["info"]["serial"]
    if data.get("new_ip"):
        dev = await db.devices.find_one({"id": data.get("device_id"), "company_id": company["id"]}, {"ip": 1})
        if dev and dev.get("ip") != data["new_ip"]:
            upd["ip"] = data["new_ip"]
            upd["ip_changed"] = {"from": dev.get("ip"), "to": data["new_ip"], "at": utcnow_iso()}
    await db.devices.update_one({"id": data.get("device_id"), "company_id": company["id"]}, {"$set": upd})
    return {"ok": True}


@router.post("/agent/push-done")
async def agent_push_done(data: dict = Body(...), company=Depends(agent_company)):
    res = {k: data.get(k) for k in ("updated", "missing", "same", "error")}
    res["at"] = utcnow_iso()
    await db.devices.update_one({"id": data.get("device_id"), "company_id": company["id"]},
                                {"$set": {"push_result": res}, "$unset": {"pending.push_names": ""}})
    return {"ok": True}
