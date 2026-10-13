"""
Hikvision access-control / face terminal client (ISAPI over HTTP, digest auth).

Works with DS-K1T341, DS-K1T671, DS-K1T804, DS-K1A802 and other Hikvision
MinMoe / Value-series terminals that expose /ISAPI/AccessControl.
"""
import re
import uuid
import datetime
import requests
import urllib3
from requests.auth import HTTPDigestAuth

urllib3.disable_warnings()

# Access-control "minor" event codes that mean a person was verified.
ATTENDANCE_MINORS = [75, 38, 1, 8, 104, 76, 9, 39]


class HikError(Exception):
    pass


class HikClient:
    def __init__(self, ip, port=80, username="admin", password="", https=False, timeout=20):
        scheme = "https" if https or int(port or 80) == 443 else "http"
        self.base = f"{scheme}://{ip}:{int(port or 80)}"
        self.auth = HTTPDigestAuth(username or "admin", password or "")
        self.timeout = timeout
        self.s = requests.Session()

    def _req(self, method, path, **kw):
        try:
            r = self.s.request(method, self.base + path, auth=self.auth, timeout=self.timeout, verify=False, **kw)
        except requests.RequestException as e:
            raise HikError(f"Cannot reach {self.base} ({e.__class__.__name__})")
        if r.status_code == 401:
            raise HikError("Wrong username / password for the Hikvision device")
        return r

    def device_info(self):
        r = self._req("GET", "/ISAPI/System/deviceInfo")
        if r.status_code != 200:
            raise HikError(f"deviceInfo failed (HTTP {r.status_code})")
        txt = r.text

        def tag(t):
            m = re.search(rf"<{t}>(.*?)</{t}>", txt)
            return m.group(1) if m else None

        return {"model": tag("model"), "serial": tag("serialNumber"), "name": tag("deviceName"),
                "firmware": tag("firmwareVersion")}

    def device_time(self):
        r = self._req("GET", "/ISAPI/System/time")
        m = re.search(r"<localTime>(.*?)</localTime>", r.text or "")
        return m.group(1) if m else None

    def _search(self, start, end, minor):
        sid = uuid.uuid4().hex
        pos = 0
        while True:
            body = {"AcsEventCond": {"searchID": sid, "searchResultPosition": pos, "maxResults": 30,
                                     "major": 5, "minor": minor,
                                     "startTime": start.isoformat(timespec="seconds"),
                                     "endTime": end.isoformat(timespec="seconds")}}
            r = self._req("POST", "/ISAPI/AccessControl/AcsEvent?format=json", json=body)
            if r.status_code != 200:
                raise HikError(f"AcsEvent HTTP {r.status_code}: {r.text[:200]}")
            ev = r.json().get("AcsEvent", {})
            items = ev.get("InfoList") or []
            for e in items:
                yield e
            pos += len(items)
            if ev.get("responseStatusStrg") != "MORE" or not items:
                break

    def get_events(self, start: datetime.datetime, end: datetime.datetime):
        """start/end must be timezone-aware datetimes. Returns list of punch dicts."""
        out, seen = [], set()
        try:
            events = list(self._search(start, end, 0))
        except HikError:
            events = []
            for m in ATTENDANCE_MINORS:
                try:
                    events.extend(self._search(start, end, m))
                except HikError:
                    pass
        for e in events:
            emp = e.get("employeeNoString") or (str(e.get("employeeNo")) if e.get("employeeNo") else "")
            if not emp or not e.get("time"):
                continue
            key = (emp, e["time"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"user_id": emp, "ts": e["time"], "name": e.get("name"),
                        "attendance_status": e.get("attendanceStatus"),
                        "verify": e.get("currentVerifyMode")})
        return out

    def get_users(self):
        users, pos = [], 0
        sid = uuid.uuid4().hex
        while True:
            body = {"UserInfoSearchCond": {"searchID": sid, "searchResultPosition": pos, "maxResults": 30}}
            r = self._req("POST", "/ISAPI/AccessControl/UserInfo/Search?format=json", json=body)
            if r.status_code != 200:
                raise HikError(f"UserInfo HTTP {r.status_code}")
            res = r.json().get("UserInfoSearch", {})
            items = res.get("UserInfo") or []
            for u in items:
                users.append({"user_id": str(u.get("employeeNo")), "name": u.get("name") or str(u.get("employeeNo"))})
            pos += len(items)
            if res.get("responseStatusStrg") != "MORE" or not items:
                break
        return users


    def update_names(self, wanted):
        """wanted: {employeeNo: name}. Renames users that exist on the terminal."""
        existing = {u["user_id"]: u["name"] for u in self.get_users()}
        updated, missing, same = [], [], []
        for uid, nm in wanted.items():
            uid = str(uid)
            if uid not in existing:
                missing.append(uid)
                continue
            nm = (nm or "").strip()[:32]
            if existing[uid] == nm:
                same.append(uid)
                continue
            body = {"UserInfo": {"employeeNo": uid, "name": nm}}
            r = self._req("PUT", "/ISAPI/AccessControl/UserInfo/Modify?format=json", json=body)
            if r.status_code != 200:
                raise HikError(f"Could not rename {uid} (HTTP {r.status_code}: {r.text[:120]})")
            updated.append(uid)
        return updated, missing, same

    # ---------- face photos ----------
    def _face_libs(self):
        libs = []
        try:
            r = self._req("GET", "/ISAPI/Intelligent/FDLib?format=json")
            if r.status_code == 200:
                j = r.json()
                items = j.get("FDLib") or j.get("FDLibBaseCfgList") or []
                if isinstance(items, dict):
                    items = [items]
                for it in items:
                    it = it.get("FDLibBaseCfg", it) if isinstance(it, dict) else {}
                    if it.get("FDID"):
                        libs.append((str(it["FDID"]), it.get("faceLibType") or "blackFD"))
        except Exception:
            pass
        return libs or [("1", "blackFD")]

    def get_faces(self):
        """Returns [{user_id, url}] for every enrolled face picture."""
        out, seen = [], set()
        for fdid, ftype in self._face_libs():
            pos = 0
            while True:
                body = {"searchResultPosition": pos, "maxResults": 30, "faceLibType": ftype, "FDID": fdid}
                r = self._req("POST", "/ISAPI/Intelligent/FDLib/FDSearch?format=json", json=body)
                if r.status_code != 200:
                    break
                j = r.json()
                items = j.get("MatchList") or []
                for m in items:
                    uid, url = str(m.get("FPID") or ""), m.get("faceURL") or m.get("modelData")
                    if uid and url and uid not in seen and str(url).startswith("http"):
                        seen.add(uid)
                        out.append({"user_id": uid, "url": url})
                pos += len(items)
                if j.get("responseStatusStrg") != "MORE" or not items:
                    break
        return out

    def download(self, url):
        """Face URLs carry the device's own LAN address; fetch them through the address we reach the device on."""
        import urllib.parse as up
        u = up.urlsplit(url)
        path = u.path + (("?" + u.query) if u.query else "")
        r = self._req("GET", path)
        if r.status_code != 200 or not r.content:
            raise HikError(f"Photo download failed (HTTP {r.status_code})")
        return r.content


def test_device(ip, port=80, username="admin", password=""):
    h = HikClient(ip, port, username, password, timeout=10)
    info = h.device_info()
    info["device_time"] = h.device_time()
    info["ok"] = True
    return info
