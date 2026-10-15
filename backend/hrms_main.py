"""Hayat HRMS — Biometric attendance, payroll & WhatsApp. Run: uvicorn server:app --host 0.0.0.0 --port 8001"""
import os
import sys
import asyncio
from pathlib import Path

# Lets Vercel import this file as "backend.server" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.config import CORS_ORIGINS, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, DISABLE_SCHEDULER, APP_NAME
from app.db import db, ensure_indexes
from app.auth import hash_pw
from app.utils import new_id, utcnow_iso
from app.routers import core, hr, devices, work, shifts_roster, license as license_router, relay as relay_router, me as me_router
from app.services import license as lic
from app.services.scheduler import run_scheduler, tick_all

app = FastAPI(title=APP_NAME)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in CORS_ORIGINS.split(",")], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(core.router)
app.include_router(me_router.router)
app.include_router(hr.router)
app.include_router(devices.router)
app.include_router(work.router)
app.include_router(shifts_roster.router)
app.include_router(license_router.router)
app.include_router(relay_router.router)

# allowed without a licence: activation, login (so the super admin can reach the Licence page), health
LICENSE_FREE = ("/api/license/", "/api/health", "/api/cron/", "/api/admin/licenses", "/api/auth/", "/api/relay/")


@app.middleware("http")
async def license_gate(request: Request, call_next):
    """Customer computers without a valid licence key: the page shows the demo panel and the API refuses real work."""
    path = request.url.path
    if (path.startswith("/api/") or path.startswith("/iclock")) and not path.startswith(LICENSE_FREE) \
            and request.method != "OPTIONS" and lic.license_required():
        try:
            st = await lic.status()
        except Exception as e:  # errors here skip the normal handlers, so answer clearly ourselves
            return await server_error(request, e)
        if not st["licensed"]:
            return JSONResponse({"detail": "Licence required: " + (st["reason"] or "activate this computer"),
                                 "license_required": True}, status_code=402)
    return await call_next(request)
# eSSL / ZKTeco ADMS push. Devices always call /iclock/...; /api/iclock is for hosts that only route /api to the backend.
app.include_router(devices.iclock, prefix="/iclock")
app.include_router(devices.iclock, prefix="/api/iclock")


@app.get("/api/cron/tick")
async def cron_tick(request: Request, key: str = ""):
    """Runs the scheduled jobs once (WhatsApp summary/absent alerts, static-IP pulls, offline marking).
    Used on Vercel (Vercel Cron or cron-job.org). Protected by CRON_SECRET."""
    secret = os.environ.get("CRON_SECRET", "")
    given = key or request.headers.get("authorization", "").replace("Bearer ", "")
    if not secret or given != secret:
        raise HTTPException(401, "Set CRON_SECRET and call with ?key=<secret>")
    n = await tick_all()
    return {"ok": True, "companies": n}


APP_VERSION = "2.3"


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    """Never answer with a bare 500 — say what went wrong so it can be fixed from the screen."""
    import traceback
    traceback.print_exc()
    name = exc.__class__.__name__
    if str(exc).startswith("Database not configured"):
        return JSONResponse({"detail": str(exc)}, 503)
    if name in ("OperationFailure",) and "auth" in str(exc).lower():
        return JSONResponse({"detail": "Database login failed — check the user name and password inside MONGO_URL "
                                       "(MongoDB Atlas → Database Access)."}, 503)
    if name in ("ServerSelectionTimeoutError", "AutoReconnect", "NetworkTimeout", "ConnectionFailure", "ConfigurationError"):
        return JSONResponse({"detail": "Database (MongoDB) is not running or not reachable. On the PC: double-click "
                                       "'Stop Hayat HRMS', then open 'Hayat HRMS' again. Details: " + str(exc)[:160]}, 503)
    return JSONResponse({"detail": f"Server error ({name}): {str(exc)[:300]} — see local\\app.log"}, 500)


@app.get("/api/health")
async def health(db_check: int = 1):
    """Open https://<site>/api/health to see if the server and the database are fine."""
    from app.db import DB_ERROR
    out = {"ok": True, "app": APP_NAME, "version": APP_VERSION, "database": "ok"}
    if DB_ERROR:
        out.update(ok=False, database=DB_ERROR)
    elif db_check:
        try:
            await db.command("ping")
        except Exception as e:
            out.update(ok=False, database=f"cannot connect: {e.__class__.__name__}: {str(e)[:220]}. In MongoDB Atlas → "
                                           "Network Access allow 0.0.0.0/0, and check the user name / password in MONGO_URL.")
    return out


# Serve the built React app from the same server when it exists (local / VPS single-process install).
FRONTEND_BUILD = Path(__file__).resolve().parent.parent / "frontend" / "build"
if FRONTEND_BUILD.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_BUILD / "static"), name="static")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        f = FRONTEND_BUILD / full_path
        if full_path and f.is_file():
            return FileResponse(f)
        return FileResponse(FRONTEND_BUILD / "index.html")


@app.on_event("startup")
async def startup():
    from app.bootstrap import ensure_ready
    try:
        await ensure_ready()
    except Exception as e:  # e.g. database not reachable yet: the site still starts, /api/health tells why
        print("startup warning:", e)
    # On Vercel nothing runs between requests, so the cron URL above replaces the loop.
    if not DISABLE_SCHEDULER and not os.environ.get("VERCEL"):
        asyncio.create_task(run_scheduler())
