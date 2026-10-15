import datetime
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .config import JWT_SECRET, JWT_HOURS
from .db import db

bearer = HTTPBearer(auto_error=False)


def hash_pw(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def check_pw(p, h):
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def make_token(user):
    payload = {"sub": user["id"], "role": user["role"], "tv": int(user.get("token_version") or 0),
               "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=JWT_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


class Ctx:
    def __init__(self, user, company):
        self.user = user
        self.company = company

    @property
    def cid(self):
        return self.company["id"] if self.company else None

    @property
    def role(self):
        return self.user["role"]


async def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)):
    if not cred:
        raise HTTPException(401, "Login required")
    try:
        data = jwt.decode(cred.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please login again")
    user = await db.users.find_one({"id": data["sub"]}, {"_id": 0, "password_hash": 0})
    if not user or not user.get("active", True):
        raise HTTPException(401, "User disabled")
    if int(data.get("tv") or 0) != int(user.get("token_version") or 0):
        raise HTTPException(401, "You were logged out. Please sign in again.")
    msg = login_expired(user)
    if msg:
        raise HTTPException(401, msg)
    return user


def login_expired(user):
    """Each login can have its own validity date (set by the super admin / owner)."""
    vt = user.get("valid_till")
    if vt and user.get("role") != "superadmin" and vt < datetime.date.today().isoformat():
        return f"Your login expired on {vt}. Contact your administrator to renew it."
    return None


async def superadmin(user=Depends(current_user)):
    if user["role"] != "superadmin":
        raise HTTPException(403, "Super admin only")
    return user


async def company_ctx(user=Depends(current_user), x_company_id: str = Header(None)):
    """Resolves the company the request works on. Super admin picks one with X-Company-Id."""
    if user["role"] == "employee":
        raise HTTPException(403, "Employee logins can only see their own attendance and salary slips")
    return await _company(user, x_company_id)


async def employee_ctx(user=Depends(current_user)):
    """Employee self-service login: one employee, own data only."""
    if user["role"] != "employee" or not user.get("employee_id"):
        raise HTTPException(403, "Employee login only")
    return await _company(user, None)


async def _company(user, x_company_id):
    cid = x_company_id if user["role"] == "superadmin" else user.get("company_id")
    if not cid:
        raise HTTPException(400, "Select a company first")
    company = await db.companies.find_one({"id": cid}, {"_id": 0})
    if not company:
        raise HTTPException(404, "Company not found")
    if user["role"] != "superadmin":
        if company.get("pending"):
            raise HTTPException(403, "Your shop account is waiting for approval.")
        if not company.get("active", True):
            raise HTTPException(403, "This shop account is suspended. Contact Hayat Enterprises.")
        vt = company.get("valid_till")
        if vt and vt < datetime.date.today().isoformat():
            raise HTTPException(403, f"Subscription expired on {vt}. Contact Hayat Enterprises to renew.")
    return Ctx(user, company)


async def owner_ctx(ctx: Ctx = Depends(company_ctx)):
    if ctx.role not in ("superadmin", "owner"):
        raise HTTPException(403, "Only the owner can do this")
    return ctx
