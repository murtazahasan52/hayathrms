"""Licence keys.

* Your main server (with LICENSE_PRIVATE_KEY set) is the *issuer*: super admin makes / renews keys there.
* Every other install (customer's computer) needs a valid key. Without one the web page shows the demo panel
  and the API refuses real work. Keys are signed (Ed25519), so they are checked offline — the computer does
  not need internet. When it does have internet it quietly fetches the latest key from the issuer once a day,
  so a renewal you make from anywhere reaches the customer automatically.

Key = "HAYAT1." + base64url(json payload) + "." + base64url(signature)
payload: v, c (licence code), n (customer), m (machines), s (shops, 0 = unlimited), e (valid till),
         t (issued), h (install id it is locked to, optional), u (issuer URL for online renewal)
"""
import os
import json
import time
import uuid
import base64
import hashlib
import platform
import datetime
import asyncio
from . import ed25519
from ..db import db
from ..utils import utcnow_iso

PUBLIC_KEY = bytes.fromhex("cdfcac28533687437362ec3a30ad3719572a281a5f252e9c5a6e269e8be2e047")
PREFIX = "HAYAT1"


KEY_PROBLEM = ""


def _private_seed():
    """LICENSE_PRIVATE_KEY in .env — tolerant of quotes, spaces and a pasted 'LICENSE_PRIVATE_KEY=' prefix."""
    global KEY_PROBLEM
    v = os.environ.get("LICENSE_PRIVATE_KEY", "")
    v = v.split("=", 1)[1] if "=" in v else v
    v = "".join(ch for ch in v if ch not in " \t\r\n'\"")
    if not v:
        return None
    try:
        seed = bytes.fromhex(v)
    except ValueError:
        KEY_PROBLEM = "LICENSE_PRIVATE_KEY in backend\\.env is not valid (it must be 64 letters/digits 0-9 a-f)."
        return None
    if len(seed) != 32 or ed25519.public_key(seed) != PUBLIC_KEY:
        KEY_PROBLEM = "LICENSE_PRIVATE_KEY in backend\\.env does not match this software. Copy it again from Hayat-License-Private-Key.txt."
        return None
    return seed


_SEED = _private_seed()
IS_ISSUER = _SEED is not None


ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")


def set_issuer_key(raw):
    """Super admin pasted LICENSE_PRIVATE_KEY in the app: check it, switch on now, save it in backend/.env.
    Returns (ok, message)."""
    global _SEED, IS_ISSUER, KEY_PROBLEM
    old = os.environ.get("LICENSE_PRIVATE_KEY")
    os.environ["LICENSE_PRIVATE_KEY"] = raw or ""
    KEY_PROBLEM = ""
    seed = _private_seed()
    if not seed:
        msg = KEY_PROBLEM or "Paste the LICENSE_PRIVATE_KEY line from Hayat-License-Private-Key.txt"
        if old is None:
            os.environ.pop("LICENSE_PRIVATE_KEY", None)
        else:
            os.environ["LICENSE_PRIVATE_KEY"] = old
        KEY_PROBLEM = ""
        _SEED = _private_seed()
        IS_ISSUER = _SEED is not None
        return False, msg.replace("in backend\\.env ", "")
    _SEED, IS_ISSUER = seed, True
    os.environ["LICENSE_PRIVATE_KEY"] = seed.hex()
    saved = _write_env("LICENSE_PRIVATE_KEY", seed.hex())
    clear_cache()
    return True, ("Licence maker switched on and saved in backend\\.env." if saved else
                  "Licence maker switched on for now, but backend\\.env could not be written (on Vercel add "
                  "LICENSE_PRIVATE_KEY in Settings → Environment Variables), so it will be off after a restart.")


def remove_issuer_key():
    global _SEED, IS_ISSUER
    os.environ.pop("LICENSE_PRIVATE_KEY", None)
    _SEED, IS_ISSUER = None, False
    _write_env("LICENSE_PRIVATE_KEY", None)
    clear_cache()


def _write_env(name, value):
    """Set / replace / remove one line in backend/.env. Returns False if the file can't be written."""
    try:
        lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, encoding="utf-8") as f:
                lines = [l.rstrip("\r\n") for l in f]
        lines = [l for l in lines if not l.strip().startswith(name + "=")]
        if value is not None:
            lines.append(f"{name}={value}")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except OSError:
        return False


def license_required():
    if IS_ISSUER:  # the licence maker never needs a key itself
        return False
    v = os.environ.get("LICENSE_REQUIRED", "").strip()
    if v in ("0", "1"):
        return v == "1"
    if IS_ISSUER or os.environ.get("VERCEL") or os.environ.get("USE_MOCK_DB") == "1":
        return False
    return True


def install_id():
    raw = f"{uuid.getnode():012x}|{platform.node().lower()}|hayat"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12].upper()
    return f"{h[:4]}-{h[4:8]}-{h[8:]}"


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_key(payload: dict) -> str:
    if not IS_ISSUER:
        raise RuntimeError("This server cannot make licence keys (LICENSE_PRIVATE_KEY not set)")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return f"{PREFIX}.{_b64e(body)}.{_b64e(ed25519.sign(_SEED, body))}"


def parse_key(key: str):
    """Returns (payload, error). Signature is checked; expiry/computer are not."""
    key = "".join((key or "").split())
    try:
        pre, body, sig = key.split(".")
        if pre != PREFIX:
            return None, "This is not a Hayat HRMS licence key"
        raw = _b64d(body)
        if not ed25519.verify(PUBLIC_KEY, raw, _b64d(sig)):
            return None, "Licence key is not genuine (signature check failed). Copy the full key again."
        return json.loads(raw), None
    except Exception:
        return None, "Licence key is incomplete or mistyped. Copy the full key again."


def check_payload(p, today=None):
    """Returns (ok, reason)."""
    today = today or datetime.date.today()
    if p.get("h") and p["h"] != install_id():
        return False, f"This key is locked to another computer ({p['h']}). This computer is {install_id()}."
    try:
        till = datetime.date.fromisoformat(p["e"])
    except Exception:
        return False, "Licence key has no valid date"
    if till < today:
        return False, f"Licence expired on {till:%d %b %Y}. Please renew."
    return True, ""


def describe(p):
    if not p:
        return None
    till = datetime.date.fromisoformat(p["e"])
    return {"code": p.get("c"), "customer": p.get("n"), "machines": int(p.get("m") or 0), "shops": int(p.get("s") or 0),
            "valid_till": p["e"], "days_left": (till - datetime.date.today()).days, "issued": p.get("t"),
            "locked_to": p.get("h") or "", "server": p.get("u") or ""}


# ---------------- local state ----------------
_cache = {"at": 0, "status": None}


async def _doc():
    return await db.platform.find_one({"id": "license"}, {"_id": 0}) or {}


async def status(force=False):
    if not force and _cache["status"] and time.time() - _cache["at"] < 30:
        return _cache["status"]
    req = license_required()
    doc = await _doc()
    p, err = parse_key(doc.get("key")) if doc.get("key") else (None, "No licence key on this computer")
    ok, reason = (False, err)
    if p:
        ok, reason = check_payload(p)
    if doc.get("revoked"):
        ok, reason = False, "This licence was cancelled by Hayat Enterprises. Please contact them."
    st = {"required": req, "issuer": IS_ISSUER, "key_problem": KEY_PROBLEM, "valid": bool(ok) or not req, "licensed": bool(ok), "reason": "" if ok else reason,
          "license": describe(p), "install_id": install_id(), "last_check": doc.get("last_check"),
          "last_check_msg": doc.get("last_check_msg"), "machines_used": await db.devices.count_documents({}),
          "shops_used": await db.companies.count_documents({}),
          "default_server": doc.get("server") or os.environ.get("LICENSE_SERVER", "")}
    _cache.update(at=time.time(), status=st)
    return st


def clear_cache():
    _cache.update(at=0, status=None)


async def save_key(key, source="manual"):
    key = "".join((key or "").split())
    p, err = parse_key(key)
    if err:
        return False, err
    ok, reason = check_payload(p)
    if not ok:
        return False, reason
    await db.platform.update_one({"id": "license"}, {"$set": {"key": key, "saved_at": utcnow_iso(), "source": source,
                                                              "revoked": False}}, upsert=True)
    clear_cache()
    return True, describe(p)


async def fetch_online(server=None, code=None):
    """Ask the issuer for the newest key of this licence (renewals / more machines reach the customer this way)."""
    import httpx
    doc = await _doc()
    p, _ = parse_key(doc.get("key")) if doc.get("key") else (None, None)
    server = (server or (p or {}).get("u") or doc.get("server") or os.environ.get("LICENSE_SERVER", "")).rstrip("/")
    code = (code or (p or {}).get("c") or "").strip().upper()
    if not server or not code:
        return False, "Licence server address and licence code are needed"
    if not server.startswith("http"):
        server = "https://" + server
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(server + "/api/license/online", json={"code": code, "install_id": install_id(),
                                                                   "current": doc.get("key") or ""})
        data = r.json()
    except Exception as e:
        msg = f"Could not reach the licence server: {e.__class__.__name__}"
        await db.platform.update_one({"id": "license"}, {"$set": {"last_check": utcnow_iso(), "last_check_msg": msg}}, upsert=True)
        clear_cache()
        return False, msg
    now = utcnow_iso()
    if r.status_code != 200 or not data.get("key"):
        msg = data.get("detail") or "Licence server refused"
        upd = {"last_check": now, "last_check_msg": msg, "server": server}
        if data.get("revoked"):
            upd["revoked"] = True
        await db.platform.update_one({"id": "license"}, {"$set": upd}, upsert=True)
        clear_cache()
        return False, msg
    ok, info = await save_key(data["key"], "online")
    await db.platform.update_one({"id": "license"}, {"$set": {"last_check": now, "server": server,
                                                              "last_check_msg": "Up to date" if ok else info}}, upsert=True)
    clear_cache()
    return ok, info


async def auto_refresh():
    """Called by the scheduler: once a day, if this computer has internet, pick up renewals."""
    if not license_required():
        return
    doc = await _doc()
    if not doc.get("key"):
        return
    last = doc.get("last_check") or ""
    try:
        if last and (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last)).total_seconds() < 20 * 3600:
            return
    except Exception:
        pass
    await fetch_online()


async def limit_check(kind):
    """kind: 'machines' or 'shops'. Raises HTTPException when the licence does not allow one more."""
    from fastapi import HTTPException
    if not license_required():
        return
    st = await status(force=True)
    lic = st.get("license") or {}
    if not st["licensed"]:
        raise HTTPException(402, st["reason"] or "Licence required")
    if kind == "machines" and lic.get("machines") and st["machines_used"] >= lic["machines"]:
        raise HTTPException(403, f"Your licence allows {lic['machines']} biometric machine(s). "
                                 "Call Hayat Enterprises (96738 78652) to add more — the new key arrives online.")
    if kind == "shops" and lic.get("shops") and st["shops_used"] >= lic["shops"]:
        raise HTTPException(403, f"Your licence allows {lic['shops']} shop(s). Call Hayat Enterprises to add more.")
