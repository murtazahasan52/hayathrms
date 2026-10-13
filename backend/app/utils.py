import uuid
import secrets
import datetime
from zoneinfo import ZoneInfo
from .config import DEFAULT_TZ


def new_id():
    return uuid.uuid4().hex


def token(n=24):
    return secrets.token_urlsafe(n)


def tz_of(company):
    try:
        return ZoneInfo((company or {}).get("settings", {}).get("timezone") or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def now_local(company=None):
    return datetime.datetime.now(tz_of(company)).replace(tzinfo=None)


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_date(s):
    return datetime.date.fromisoformat(str(s)[:10])


def daterange(d1, d2):
    d = d1
    while d <= d2:
        yield d
        d += datetime.timedelta(days=1)


def month_bounds(month):  # "2026-10"
    y, m = map(int, month.split("-"))
    first = datetime.date(y, m, 1)
    nxt = datetime.date(y + (m // 12), m % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)


def hm(t):  # "09:30" -> minutes
    h, m = str(t or "0:0").split(":")[:2]
    return int(h) * 60 + int(m)


def fmt_hm(minutes):
    minutes = int(round(minutes or 0))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clean_phone(p):
    digits = "".join(c for c in str(p or "") if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else digits


def strip(doc):
    if doc:
        doc.pop("_id", None)
    return doc


ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
        "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n):
    return ONES[n] if n < 20 else (TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else ""))


def _three(n):
    s = ""
    if n >= 100:
        s = ONES[n // 100] + " Hundred"
        n %= 100
        if n:
            s += " "
    return s + (_two(n) if n else "")


def rupees_in_words(amount):
    n = int(round(amount or 0))
    if n == 0:
        return "Rupees Zero Only"
    parts = []
    for div, name in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand")):
        if n >= div:
            parts.append(_three(n // div) + " " + name)
            n %= div
    if n:
        parts.append(_three(n))
    return "Rupees " + " ".join(parts) + " Only"
