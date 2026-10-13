"""Hayat HRMS — Biometric attendance, payroll & WhatsApp. Run: uvicorn server:app --host 0.0.0.0 --port 8001

The real app lives in hrms_main.py. This wrapper makes sure that if something stops it from starting
(a wrong setting, a missing package on the host), the browser shows the reason instead of a blank
"FUNCTION_INVOCATION_FAILED" / 500 page."""
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from hrms_main import app  # noqa: F401
except Exception:  # pragma: no cover - only when the app cannot start
    _tb = traceback.format_exc()
    _tb = re.sub(r"mongodb(\+srv)?://[^\s'\"]+", "mongodb://***hidden***", _tb)  # never show the DB password
    print(_tb)
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    app = FastAPI(title="Hayat HRMS (start-up error)")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def _startup_error(path: str = ""):
        return PlainTextResponse("Hayat HRMS could not start. Send this to Hayat Enterprises (96738 78652):\n\n" + _tb,
                                 status_code=500)
