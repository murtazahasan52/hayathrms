"""Direct pull from devices reachable from the server (public / static IP with port forwarding)."""
import asyncio
import datetime
from ..db import db
from ..utils import tz_of, now_local, utcnow_iso, new_id
from .zkclient import ZKClient, ZKError, find_moved
from .hikclient import HikClient
from .ingest import ingest


def _zk_pull(dev):
    with ZKClient(dev["ip"], dev.get("port") or 4370, dev.get("comm_key") or 0, timeout=20) as z:
        serial = ""
        try:
            serial = z.get_serial()
        except Exception:
            pass
        users = z.get_users()
        logs = z.get_attendance(users)
    return users, logs, serial


async def relocate(device):
    """eSSL machine stopped answering: if we know its serial number, look for it on the same network.
    Returns the new IP (and saves it) or None."""
    if device.get("brand") == "hikvision" or not device.get("hw_serial") or not device.get("ip"):
        return None
    new_ip = await asyncio.to_thread(find_moved, device["ip"], device["hw_serial"], device.get("port") or 4370,
                                     device.get("comm_key") or 0)
    if new_ip and new_ip != device["ip"]:
        await db.devices.update_one({"id": device["id"]}, {"$set": {"ip": new_ip, "ip_changed": {
            "from": device["ip"], "to": new_ip, "at": utcnow_iso()}}})
        device["ip"] = new_ip
        return new_ip
    return None


def _hik_pull(dev, start, end):
    h = HikClient(dev["ip"], dev.get("port") or 80, dev.get("username") or "admin", dev.get("password") or "")
    return h.get_users(), h.get_events(start, end)


def _hik_faces(dev, skip, limit=300):
    h = HikClient(dev["ip"], dev.get("port") or 80, dev.get("username") or "admin", dev.get("password") or "")
    out, errors = [], 0
    for f in h.get_faces():
        if f["user_id"] in skip:
            continue
        try:
            out.append((f["user_id"], h.download(f["url"])))
        except Exception:
            errors += 1
        if len(out) >= limit:
            break
    return out, errors


async def pull_photos(company, device, force=False):
    """Hikvision: read enrolled face photos. Returns number saved."""
    from .photos import save_photo, codes_with_photos
    if device.get("brand") != "hikvision":
        return 0
    skip = set() if force else set(await codes_with_photos(company["id"]))
    got, _ = await asyncio.to_thread(_hik_faces, device, skip)
    n = 0
    for uid, raw in got:
        n += bool(await save_photo(company, uid, raw, "hikvision"))
    return n


async def save_device_users(company, device, users):
    from .sync import auto_add_employees, linked_codes
    ignored = set(company.get("ignored_codes") or [])
    linked = await linked_codes(company["id"])
    pics = {p["code"]: p.get("updated_at") async for p in db.photos.find({"company_id": company["id"]}, {"code": 1, "updated_at": 1})}
    for u in users:
        code = str(u["user_id"])
        if code in ignored or code in linked:
            continue
        extra = {"photo_v": pics[code]} if code in pics else {}
        await db.device_users.update_one(
            {"company_id": company["id"], "user_code": code},
            {"$set": {"name": u.get("name") or "", "device_id": device["id"], "device_name": device.get("name"), **extra},
             "$setOnInsert": {"id": new_id()}}, upsert=True)
    return await auto_add_employees(company)


async def pull_device(company, device, days_back=None, photos=False):
    tz = tz_of(company)
    now = now_local(company)
    last = device.get("last_punch_at")
    if days_back:
        since = now - datetime.timedelta(days=days_back)
    elif last:
        since = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S") - datetime.timedelta(hours=6)
    else:
        since = now - datetime.timedelta(days=int(device.get("initial_days") or 45))
    try:
        if device["brand"] == "hikvision":
            users, logs = await asyncio.to_thread(_hik_pull, device, since.replace(tzinfo=tz),
                                                  (now + datetime.timedelta(hours=1)).replace(tzinfo=tz))
        else:
            try:
                users, logs, serial = await asyncio.to_thread(_zk_pull, device)
            except ZKError as e:
                if "Cannot reach" not in str(e) or not await relocate(device):
                    raise
                users, logs, serial = await asyncio.to_thread(_zk_pull, device)
            if serial and serial != device.get("hw_serial"):
                await db.devices.update_one({"id": device["id"]}, {"$set": {"hw_serial": serial}})
                device["hw_serial"] = serial
            logs = [l for l in logs if l["ts"] >= since]
        await save_device_users(company, device, users)
        n = await ingest(company, device, logs, "direct")
        await db.devices.update_one({"id": device["id"]}, {"$set": {"last_sync": utcnow_iso(), "sync_error": None,
                                                                   "status": "online", "device_users": len(users)}})
        pics = 0
        if photos or device.get("brand") == "hikvision":
            try:
                pics = await pull_photos(company, device, force=bool(photos == "all"))
            except Exception:
                pics = 0
        return {"ok": True, "new_punches": n, "fetched": len(logs), "users": len(users), "photos": pics}
    except Exception as e:
        msg = f"{e}"
        await db.devices.update_one({"id": device["id"]}, {"$set": {"last_sync": utcnow_iso(), "sync_error": msg,
                                                                   "status": "error"}})
        return {"ok": False, "error": msg}
