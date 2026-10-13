"""Auth, super-admin company management, company profile/settings/users, dashboard."""
import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from ..db import db
from ..auth import hash_pw, check_pw, make_token, current_user, superadmin, company_ctx, owner_ctx, Ctx
from ..defaults import company_settings, whatsapp_settings, DEFAULT_SHIFT, WHATSAPP_EVENTS
from ..utils import new_id, token, utcnow_iso, now_local, strip
from ..services.attendance import process_range

router = APIRouter(prefix="/api")


# ---------------- auth ----------------
@router.post("/auth/login")
async def login(data: dict = Body(...)):
    from ..bootstrap import ensure_ready
    await ensure_ready()
    email = (data.get("email") or "").strip().lower()
    u = await db.users.find_one({"email": email})
    if not u or not check_pw(data.get("password") or "", u.get("password_hash", "")):
        raise HTTPException(401, "Wrong email or password")
    if not u.get("active", True):
        raise HTTPException(403, "Your login is disabled")
    strip(u)
    u.pop("password_hash", None)
    return {"token": make_token(u), "user": u}


@router.get("/auth/me")
async def me(user=Depends(current_user)):
    company = None
    if user.get("company_id"):
        company = await db.companies.find_one({"id": user["company_id"]}, {"_id": 0, "id": 1, "name": 1, "logo": 1, "valid_till": 1, "source": 1, "pending": 1, "active": 1})
    return {"user": user, "company": company}


@router.post("/auth/logout-all")
async def logout_all(user=Depends(current_user)):
    """Ends every session of this login (all browsers / phones). Returns a fresh token for this browser."""
    await db.users.update_one({"id": user["id"]}, {"$inc": {"token_version": 1}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "password_hash": 0})
    return {"ok": True, "token": make_token(u)}


@router.post("/auth/change-password")
async def change_password(data: dict = Body(...), user=Depends(current_user)):
    u = await db.users.find_one({"id": user["id"]})
    if not check_pw(data.get("old_password") or "", u["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    if len(data.get("new_password") or "") < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_pw(data["new_password"])},
                                                    "$inc": {"token_version": 1}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "password_hash": 0})
    return {"ok": True, "token": make_token(u)}


# ---------------- super admin ----------------
async def create_company_doc(data):
    from ..services.license import limit_check
    await limit_check("shops")
    cid = new_id()
    company = {
        "id": cid, "name": data["name"].strip(), "address": data.get("address", ""), "phone": data.get("phone", ""),
        "email": data.get("email", ""), "gstin": data.get("gstin", ""), "logo": None, "active": True,
        "max_devices": int(data.get("max_devices") or 2), "max_employees": int(data.get("max_employees") or 50),
        "valid_till": data.get("valid_till") or None, "agent_key": token(24), "settings": {}, "whatsapp": {},
        "created_at": utcnow_iso(), "source": data.get("source", "admin"), "city": data.get("city", ""),
    }
    await db.companies.insert_one(dict(company))
    await db.shifts.insert_one(dict(DEFAULT_SHIFT, id=new_id(), company_id=cid))
    return company


# ---------------- shop sign-up ----------------
PLATFORM_DEFAULTS = {"allow_signup": True, "require_approval": True, "trial_days": 15, "default_max_devices": 2, "default_max_employees": 25,
                     "support_phone": "96738 78652", "brand": "Hayat HRMS"}


async def platform():
    p = await db.platform.find_one({"id": "main"}, {"_id": 0}) or {}
    return {**PLATFORM_DEFAULTS, **{k: v for k, v in p.items() if k != "id"}}


@router.get("/public/signup-info")
async def signup_info():
    p = await platform()
    return {k: p[k] for k in ("allow_signup", "require_approval", "trial_days", "support_phone", "brand")}


@router.post("/auth/register-shop")
async def register_shop(request: Request, data: dict = Body(...)):
    p = await platform()
    if not p["allow_signup"]:
        raise HTTPException(403, f"New sign-ups are closed. Call {p['support_phone']} to open a shop account.")
    shop = (data.get("shop_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    mobile = "".join(ch for ch in str(data.get("mobile") or "") if ch.isdigit())[-10:]
    pw = data.get("password") or ""
    if len(shop) < 2:
        raise HTTPException(400, "Enter your shop name")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Enter a valid email — it is your login")
    if len(mobile) != 10:
        raise HTTPException(400, "Enter a 10-digit mobile number")
    if len(pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "This email already has an account. Sign in instead.")
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    hour_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    if ip and await db.signup_log.count_documents({"ip": ip, "at": {"$gte": hour_ago}}) >= 5:
        raise HTTPException(429, "Too many sign-ups from this network. Try again in an hour.")
    valid = (datetime.date.today() + datetime.timedelta(days=int(p["trial_days"]))).isoformat() if int(p["trial_days"] or 0) else None
    company = await create_company_doc({"name": shop, "phone": mobile, "email": email, "city": data.get("city", ""),
                                        "address": data.get("city", ""), "max_devices": p["default_max_devices"],
                                        "max_employees": p["default_max_employees"], "valid_till": valid, "source": "signup"})
    if p["require_approval"]:
        await db.companies.update_one({"id": company["id"]}, {"$set": {"pending": True, "active": False}})
    user = {"id": new_id(), "email": email, "name": (data.get("owner_name") or "Owner").strip(), "role": "owner",
            "company_id": company["id"], "active": True, "phone": mobile, "password_hash": hash_pw(pw), "created_at": utcnow_iso()}
    await db.users.insert_one(dict(user))
    await db.signup_log.insert_one({"ip": ip, "at": utcnow_iso(), "company_id": company["id"]})
    user.pop("password_hash")
    return {"token": make_token(user), "user": user, "trial_till": valid, "pending": bool(p["require_approval"])}


@router.post("/admin/companies/{cid}/approve")
async def approve_shop(cid: str, _=Depends(superadmin)):
    c = await db.companies.find_one({"id": cid}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Shop not found")
    upd = {"pending": False, "active": True, "approved_at": utcnow_iso()}
    p = await platform()
    if c.get("valid_till") and int(p["trial_days"] or 0):
        # trial starts on approval, not on sign-up
        upd["valid_till"] = (datetime.date.today() + datetime.timedelta(days=int(p["trial_days"]))).isoformat()
    await db.companies.update_one({"id": cid}, {"$set": upd})
    return {"ok": True, "valid_till": upd.get("valid_till")}


@router.get("/admin/platform")
async def get_platform(_=Depends(superadmin)):
    return await platform()


@router.put("/admin/platform")
async def put_platform(data: dict = Body(...), _=Depends(superadmin)):
    upd = {k: data[k] for k in PLATFORM_DEFAULTS if k in data}
    for k in ("trial_days", "default_max_devices", "default_max_employees"):
        if k in upd:
            upd[k] = int(upd[k] or 0)
    await db.platform.update_one({"id": "main"}, {"$set": upd}, upsert=True)
    return await platform()


@router.get("/admin/companies")
async def list_companies(_=Depends(superadmin)):
    out = []
    async for c in db.companies.find({}, {"_id": 0, "logo": 0}).sort("created_at", -1):
        c["employees"] = await db.employees.count_documents({"company_id": c["id"], "active": {"$ne": False}})
        c["devices"] = await db.devices.count_documents({"company_id": c["id"]})
        owner = await db.users.find_one({"company_id": c["id"], "role": "owner"}, {"_id": 0, "email": 1, "name": 1})
        c["owner"] = owner
        lic = await db.licenses.find_one({"company_id": c["id"]}, {"_id": 0, "code": 1, "valid_till": 1, "status": 1, "install_id": 1, "machines": 1})
        c["license"] = lic
        out.append(c)
    return out


@router.post("/admin/companies")
async def add_company(data: dict = Body(...), _=Depends(superadmin)):
    if not data.get("name") or not data.get("owner_email") or not data.get("owner_password"):
        raise HTTPException(400, "Company name, owner email and password are required")
    email = data["owner_email"].strip().lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "This email is already used")
    company = await create_company_doc(data)
    await db.users.insert_one({"id": new_id(), "email": email, "name": data.get("owner_name") or "Owner",
                               "role": "owner", "company_id": company["id"], "active": True,
                               "password_hash": hash_pw(data["owner_password"]), "created_at": utcnow_iso()})
    return strip(company)


@router.put("/admin/companies/{cid}")
async def edit_company(cid: str, data: dict = Body(...), _=Depends(superadmin)):
    allowed = {k: data[k] for k in ("name", "active", "max_devices", "max_employees", "valid_till", "phone", "address", "email")
               if k in data}
    await db.companies.update_one({"id": cid}, {"$set": allowed})
    if "valid_till" in allowed or "max_devices" in allowed:
        from .license import sync_from_shop
        await sync_from_shop(cid, allowed.get("valid_till"), allowed.get("max_devices"))
    if data.get("owner_password"):
        await db.users.update_one({"company_id": cid, "role": "owner"},
                                  {"$set": {"password_hash": hash_pw(data["owner_password"])}})
    return {"ok": True}


@router.post("/admin/companies/{cid}/delete")
async def delete_company(cid: str, data: dict = Body(...), _=Depends(superadmin)):
    c = await db.companies.find_one({"id": cid})
    if not c:
        raise HTTPException(404, "Company not found")
    if (data.get("confirm_name") or "").strip().lower() != c["name"].strip().lower():
        raise HTTPException(400, "Type the company name exactly to confirm")
    for coll in ("employees", "attendance", "punches", "deleted_punches", "devices", "device_users", "shifts", "holidays",
                 "leaves", "advances", "payslips", "whatsapp_logs", "users"):
        await db[coll].delete_many({"company_id": cid})
    await db.companies.delete_one({"id": cid})
    return {"ok": True}


@router.get("/admin/unknown-devices")
async def unknown_devices(_=Depends(superadmin)):
    return await db.unknown_devices.find({}, {"_id": 0}).sort("last_seen", -1).to_list(200)


# ---------------- company ----------------
@router.get("/company")
async def get_company(ctx: Ctx = Depends(company_ctx)):
    c = dict(ctx.company)
    c.pop("sched_flags", None)
    c["settings"] = company_settings(ctx.company)
    w = whatsapp_settings(ctx.company)
    if ctx.role not in ("owner", "superadmin"):
        w["password"] = ""
        c["agent_key"] = ""
    c["whatsapp"] = w
    c["whatsapp_events_meta"] = WHATSAPP_EVENTS
    return c


def _shrink_logo(data_url):
    """Accept any picture; store a small PNG (keeps transparency) so slips and pages stay light."""
    import base64, io
    from PIL import Image
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1] if "," in data_url else data_url)
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise HTTPException(400, "Logo must be a PNG or JPG picture")
    im = im.convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((600, 300))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    if out.tell() > 400_000:
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


@router.put("/company/profile")
async def update_profile(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    allowed = {k: data[k] for k in ("name", "address", "phone", "email", "gstin", "logo") if k in data}
    if allowed.get("logo"):
        allowed["logo"] = _shrink_logo(allowed["logo"])
    await db.companies.update_one({"id": ctx.cid}, {"$set": allowed})
    return {"ok": True}


@router.put("/company/settings")
async def update_settings(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    s = company_settings(ctx.company)
    s.update({k: v for k, v in data.items() if k in s})
    await db.companies.update_one({"id": ctx.cid}, {"$set": {"settings": s}})
    return s


@router.post("/company/regen-agent-key")
async def regen_agent_key(ctx: Ctx = Depends(owner_ctx)):
    k = token(24)
    await db.companies.update_one({"id": ctx.cid}, {"$set": {"agent_key": k}})
    return {"agent_key": k}


@router.get("/company/users")
async def company_users(ctx: Ctx = Depends(owner_ctx)):
    return await db.users.find({"company_id": ctx.cid}, {"_id": 0, "password_hash": 0}).to_list(200)


@router.post("/company/users")
async def add_user(data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    email = (data.get("email") or "").strip().lower()
    if not email or len(data.get("password") or "") < 6:
        raise HTTPException(400, "Email and a 6+ character password are required")
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "This email is already used")
    role = data.get("role") if data.get("role") in ("owner", "manager") else "manager"
    u = {"id": new_id(), "email": email, "name": data.get("name") or email, "role": role, "company_id": ctx.cid,
         "active": True, "password_hash": hash_pw(data["password"]), "created_at": utcnow_iso()}
    await db.users.insert_one(dict(u))
    u.pop("password_hash")
    return u


@router.put("/company/users/{uid}")
async def edit_user(uid: str, data: dict = Body(...), ctx: Ctx = Depends(owner_ctx)):
    upd = {k: data[k] for k in ("name", "active", "role") if k in data}
    if data.get("password"):
        upd["password_hash"] = hash_pw(data["password"])
    if uid == ctx.user["id"]:
        upd.pop("active", None)
        upd.pop("role", None)
    await db.users.update_one({"id": uid, "company_id": ctx.cid}, {"$set": upd})
    return {"ok": True}


# ---------------- dashboard ----------------
@router.get("/dashboard")
async def dashboard(ctx: Ctx = Depends(company_ctx)):
    now = now_local(ctx.company)
    today = now.date()
    await process_range(ctx.company, today, today)
    rows = await db.attendance.find({"company_id": ctx.cid, "date": today.isoformat()}, {"_id": 0}).to_list(10000)
    count = lambda *s: sum(1 for r in rows if r["status"] in s)
    stats = {"total": len(rows), "present": count("P", "HD", "MIS", "IN"), "inside": count("IN"),
             "absent": count("A"), "not_yet": count("-"), "on_leave": count("L", "LWP"), "off": count("WO", "H"),
             "late": sum(1 for r in rows if (r.get("late_minutes") or 0) > 0)}
    devices = await db.devices.find({"company_id": ctx.cid}, {"_id": 0, "password": 0}).to_list(100)
    from .devices import live_status
    for dv in devices:
        dv["status"] = live_status(dv)
    recent = await db.punches.find({"company_id": ctx.cid}, {"_id": 0}).sort("ts", -1).to_list(15)
    names = {}
    async for e in db.employees.find({"company_id": ctx.cid}, {"_id": 0, "code": 1, "name": 1}):
        names[e["code"]] = e["name"]
    for p in recent:
        p["employee_name"] = names.get(p["emp_code"], "Unregistered ID")
    trend = []
    for i in range(6, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        pr = await db.attendance.count_documents({"company_id": ctx.cid, "date": d, "status": {"$in": ["P", "HD", "MIS", "IN"]}})
        ab = await db.attendance.count_documents({"company_id": ctx.cid, "date": d, "status": "A"})
        trend.append({"date": d, "present": pr, "absent": ab})
    late_list = sorted([r for r in rows if (r.get("late_minutes") or 0) > 0], key=lambda r: -r["late_minutes"])[:10]
    linked = set()
    async for e in db.employees.find({"company_id": ctx.cid}, {"code": 1, "extra_codes": 1}):
        linked.add(e.get("code"))
        linked.update(e.get("extra_codes") or [])
    unregistered = 0
    async for u in db.device_users.find({"company_id": ctx.cid}, {"user_code": 1}):
        if u["user_code"] not in linked:
            unregistered += 1
    return {"date": today.isoformat(), "time": now.strftime("%H:%M"), "stats": stats, "devices": devices,
            "recent": recent, "trend": trend, "late_list": late_list, "device_user_count": unregistered}
