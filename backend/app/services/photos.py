"""Employee photos: stored small (JPEG) in their own collection, served at an unguessable public URL."""
import io
import base64
from ..db import db
from ..utils import utcnow_iso, token


def shrink(raw: bytes, size=360) -> bytes:
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()
    except Exception:
        return raw


def decode_any(data: str) -> bytes:
    if "," in data[:100] and data.startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


async def photo_key(company):
    k = company.get("photo_key")
    if not k:
        k = token(12)
        await db.companies.update_one({"id": company["id"]}, {"$set": {"photo_key": k}})
        company["photo_key"] = k
    return k


async def save_photo(company, code, raw: bytes, source="upload"):
    if not raw or len(raw) < 200:
        return False
    img = shrink(raw)
    v = utcnow_iso()
    await db.photos.update_one({"company_id": company["id"], "code": str(code)},
                               {"$set": {"data": base64.b64encode(img).decode(), "source": source, "updated_at": v}},
                               upsert=True)
    await db.employees.update_many({"company_id": company["id"], "$or": [{"code": str(code)}, {"extra_codes": str(code)}]},
                                   {"$set": {"photo_v": v}})
    await db.device_users.update_many({"company_id": company["id"], "user_code": str(code)}, {"$set": {"photo_v": v}})
    return True


async def codes_with_photos(cid):
    return [p["code"] async for p in db.photos.find({"company_id": cid}, {"code": 1})]


def photo_url(key, code, v):
    return f"/api/public/photo/{key}/{code}.jpg?v={(v or '')[-12:].replace(':', '').replace('.', '')}" if v else None
