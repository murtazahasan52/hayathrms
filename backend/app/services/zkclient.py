"""
Pure-Python client for ZKTeco / eSSL biometric devices (TCP port 4370).

Works with eSSL, ZKTeco, Identix, Realtime and most "ZK protocol" machines
(K30, X990, MB160, uFace, SpeedFace, iClock, F18, K40 ...).

No third-party dependency, so it runs on any Python 3.8+ (server or agent).
"""
import socket
import struct
import datetime

CMD_CONNECT = 1000
CMD_EXIT = 1001
CMD_ENABLEDEVICE = 1002
CMD_DISABLEDEVICE = 1003
CMD_AUTH = 1102
CMD_ACK_OK = 2000
CMD_ACK_ERROR = 2001
CMD_ACK_UNAUTH = 2005
CMD_PREPARE_DATA = 1500
CMD_DATA = 1501
CMD_FREE_DATA = 1502
CMD_DATA_WRRQ = 1503
CMD_READ_BUFFER = 1504
CMD_ATTLOG_RRQ = 13
CMD_USERTEMP_RRQ = 9
CMD_GET_FREE_SIZES = 50
CMD_GET_TIME = 201
CMD_USER_WRQ = 8
CMD_REFRESHDATA = 1013
CMD_OPTIONS_RRQ = 11
FCT_USER = 5

USHRT_MAX = 65535
MAGIC1 = 0x5050
MAGIC2 = 0x7D82
MAX_CHUNK = 0xFFC0


class ZKError(Exception):
    pass


def _checksum(buf: bytes) -> int:
    s = 0
    n = len(buf)
    i = 0
    while n > 1:
        s += struct.unpack("<H", buf[i:i + 2])[0]
        if s > USHRT_MAX:
            s -= USHRT_MAX
        i += 2
        n -= 2
    if n:
        s += buf[-1]
    while s > USHRT_MAX:
        s -= USHRT_MAX
    s = ~s
    while s < 0:
        s += USHRT_MAX
    return s


def make_commkey(key, session_id, ticks=50) -> bytes:
    key = int(key or 0)
    session_id = int(session_id)
    k = 0
    for i in range(32):
        if key & (1 << i):
            k = (k << 1) | 1
        else:
            k = k << 1
    k += session_id
    k = struct.pack("<I", k & 0xFFFFFFFF)
    b = struct.unpack("BBBB", k)
    k = struct.pack("BBBB", b[0] ^ ord("Z"), b[1] ^ ord("K"), b[2] ^ ord("S"), b[3] ^ ord("O"))
    h = struct.unpack("<HH", k)
    k = struct.pack("<HH", h[1], h[0])
    B = 0xFF & ticks
    b = struct.unpack("BBBB", k)
    return struct.pack("BBBB", b[0] ^ B, b[1] ^ B, B, b[3] ^ B)


def decode_time(t: int) -> datetime.datetime:
    second = t % 60
    t //= 60
    minute = t % 60
    t //= 60
    hour = t % 24
    t //= 24
    day = t % 31 + 1
    t //= 31
    month = t % 12 + 1
    t //= 12
    year = t + 2000
    return datetime.datetime(year, month, day, hour, minute, second)


def _clean(b: bytes) -> str:
    return b.split(b"\x00")[0].decode("utf-8", errors="ignore").strip()


class ZKClient:
    def __init__(self, ip, port=4370, password=0, timeout=15, proto="auto"):
        self.ip = ip
        self.port = int(port or 4370)
        self.password = int(password or 0)
        self.timeout = timeout
        self.proto = proto          # "auto" = try TCP, then UDP (older eSSL / ZK models speak only UDP)
        self.udp = False
        self.sock = None
        self.session = 0
        self.reply = USHRT_MAX - 1

    # ---------- low level ----------
    def _send(self, cmd, data=b""):
        buf = struct.pack("<4H", cmd, 0, self.session, self.reply) + data
        chk = _checksum(buf)
        self.reply += 1
        if self.reply >= USHRT_MAX:
            self.reply -= USHRT_MAX
        packet = struct.pack("<4H", cmd, chk, self.session, self.reply) + data
        if self.udp:
            self.sock.send(packet)
            return
        top = struct.pack("<HHI", MAGIC1, MAGIC2, len(packet))
        self.sock.sendall(top + packet)

    def _recv_exact(self, n):
        out = bytearray()
        while len(out) < n:
            part = self.sock.recv(n - len(out))
            if not part:
                raise ZKError("Device closed the connection")
            out += part
        return bytes(out)

    def _recv_packet(self):
        if self.udp:
            body = self.sock.recv(65535)
            if len(body) < 8:
                raise ZKError("Invalid response from device")
            cmd, _chk, sess, reply = struct.unpack("<4H", body[:8])
            self.reply = reply
            return cmd, sess, body[8:]
        top = self._recv_exact(8)
        m1, m2, length = struct.unpack("<HHI", top)
        if m1 != MAGIC1 or m2 != MAGIC2:
            raise ZKError("Invalid response (not a ZK TCP device?)")
        body = self._recv_exact(length)
        cmd, _chk, sess, reply = struct.unpack("<4H", body[:8])
        self.reply = reply
        return cmd, sess, body[8:]

    def _cmd(self, cmd, data=b""):
        self._send(cmd, data)
        return self._recv_packet()

    # ---------- session ----------
    def _open(self, udp):
        self.udp = udp
        if udp:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.ip, self.port))
        else:
            self.sock = socket.create_connection((self.ip, self.port), timeout=min(self.timeout, 8))
            self.sock.settimeout(self.timeout)
        self.session = 0
        self.reply = USHRT_MAX - 1
        cmd, sess, _ = self._cmd(CMD_CONNECT)
        self.session = sess
        if cmd == CMD_ACK_UNAUTH:
            cmd, _, _ = self._cmd(CMD_AUTH, make_commkey(self.password, self.session))
            if cmd != CMD_ACK_OK:
                raise ZKError("Device rejected the Comm Key (password). Set the same Comm Key as in the machine: "
                              "Menu > Comm. > Comm Key (or PC Connection).")
        elif cmd != CMD_ACK_OK:
            raise ZKError(f"Connect failed (code {cmd})")

    def _close_sock(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def connect(self):
        order = {"tcp": [False], "udp": [True]}.get(self.proto, [False, True])
        errors = []
        for udp in order:
            try:
                self._open(udp)
                return self
            except ZKError as e:
                self._close_sock()
                if "Comm Key" in str(e):
                    raise
                errors.append(f"{'UDP' if udp else 'TCP'}: {e}")
            except (OSError, struct.error) as e:
                self._close_sock()
                msg = "no reply (timed out)" if isinstance(e, socket.timeout) else str(e)
                errors.append(f"{'UDP' if udp else 'TCP'}: {msg}")
        raise ZKError(f"Cannot reach {self.ip}:{self.port} — " + "; ".join(errors) +
                      ". Check: same network/subnet as the machine, the IP on the machine (Menu > Comm. > Ethernet), "
                      "port 4370, and that no firewall blocks it.")

    def disconnect(self):
        try:
            if self.sock:
                self._cmd(CMD_EXIT)
        except Exception:
            pass
        finally:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.disconnect()

    def disable(self):
        self._cmd(CMD_DISABLEDEVICE)

    def enable(self):
        self._cmd(CMD_ENABLEDEVICE)

    # ---------- buffered reads ----------
    def _read_chunk(self, start, size):
        self._send(CMD_READ_BUFFER, struct.pack("<ii", start, size))
        data = bytearray()
        prepared = False
        while True:
            cmd, _, d = self._recv_packet()
            if cmd == CMD_PREPARE_DATA:
                prepared = True
                continue
            if cmd == CMD_DATA:
                data += d
                if len(data) >= size:
                    if prepared:
                        cmd2, _, _ = self._recv_packet()  # trailing ACK_OK
                    return bytes(data[:size])
                continue
            if cmd == CMD_ACK_OK:
                return bytes(data)
            raise ZKError(f"Unexpected reply while reading data ({cmd})")

    def _read_with_buffer(self, command, fct=0, ext=0):
        cmd, _, d = self._cmd(CMD_DATA_WRRQ, struct.pack("<bhii", 1, command, fct, ext))
        if cmd == CMD_DATA:
            return d
        if cmd not in (CMD_ACK_OK, CMD_PREPARE_DATA):
            raise ZKError(f"Read request refused ({cmd})")
        size = struct.unpack("<I", d[1:5])[0]
        out = bytearray()
        start = 0
        while start < size:
            chunk = min(16 * 1024 if self.udp else MAX_CHUNK, size - start)
            out += self._read_chunk(start, chunk)
            start += chunk
        try:
            self._cmd(CMD_FREE_DATA)
        except Exception:
            pass
        return bytes(out)

    # ---------- public API ----------
    def read_sizes(self):
        cmd, _, d = self._cmd(CMD_GET_FREE_SIZES)
        if len(d) >= 80:
            f = struct.unpack("<20i", d[:80])
            return {"users": f[4], "fingers": f[6], "records": f[8]}
        return {"users": 0, "fingers": 0, "records": 0}

    def get_option(self, name):
        cmd, _, d = self._cmd(CMD_OPTIONS_RRQ, name.encode() + b"\x00")
        if cmd != CMD_ACK_OK or b"=" not in d:
            return ""
        return d.split(b"=", 1)[1].split(b"\x00")[0].decode("utf-8", "ignore").strip()

    def get_serial(self):
        return self.get_option("~SerialNumber")

    def get_time(self):
        cmd, _, d = self._cmd(CMD_GET_TIME)
        if len(d) >= 4:
            return decode_time(struct.unpack("<I", d[:4])[0])
        return None

    def get_users(self):
        sizes = self.read_sizes()
        data = self._read_with_buffer(CMD_USERTEMP_RRQ, FCT_USER)
        if len(data) <= 4:
            return []
        total = struct.unpack("<I", data[:4])[0]
        data = data[4:4 + total]
        n = sizes.get("users") or 0
        psize = (total // n) if n else (72 if total % 72 == 0 else 28)
        users = []
        self.user_packet_size = 28 if psize == 28 else 72
        if psize == 28:
            while len(data) >= 28:
                uid, priv, pw, name, card, grp, tz, user_id = struct.unpack("<HB5s8sIxBhI", data[:28])
                users.append({"uid": uid, "user_id": str(user_id), "name": _clean(name) or str(user_id),
                              "raw_name": _clean(name), "privilege": priv, "card": card,
                              "_pw": pw, "_grp": grp, "_tz": tz})
                data = data[28:]
        else:
            while len(data) >= 72:
                uid, priv, pw, name, card, grp, user_id = struct.unpack("<HB8s24sIx7sx24s", data[:72])
                uid_s = _clean(user_id) or str(uid)
                users.append({"uid": uid, "user_id": uid_s, "name": _clean(name) or uid_s,
                              "raw_name": _clean(name), "privilege": priv, "card": card,
                              "_pw": pw, "_grp": grp})
                data = data[72:]
        return users

    def set_user(self, u, name=None, card=None):
        """Write one user back to the machine, changing only the name / card.
        `u` must come from get_users() so password, privilege and group are kept as they are."""
        nm = (u.get("raw_name") if name is None else name) or ""
        cd = int(u.get("card") or 0) if card is None else int(card or 0)
        if getattr(self, "user_packet_size", 72) == 28:
            b = nm.encode("utf-8", "ignore")[:8]
            pkt = struct.pack("<HB5s8sIxBhI", int(u["uid"]), int(u.get("privilege") or 0), u.get("_pw") or b"",
                              b, cd, int(u.get("_grp") or 0), int(u.get("_tz") or 0), int(u["user_id"]))
        else:
            b = nm.encode("utf-8", "ignore")[:23]
            pkt = struct.pack("<HB8s24sIx7sx24s", int(u["uid"]), int(u.get("privilege") or 0), u.get("_pw") or b"",
                              b, cd, u.get("_grp") or b"1", str(u["user_id"]).encode()[:24])
        cmd, _, _ = self._cmd(CMD_USER_WRQ, pkt)
        if cmd != CMD_ACK_OK:
            raise ZKError(f"Machine refused to save user {u['user_id']} ({cmd})")

    def update_names(self, wanted):
        """wanted: {user_id: name}. Updates users that exist on the machine. Returns (updated, missing, unchanged)."""
        users = {u["user_id"]: u for u in self.get_users()}
        updated, missing, same = [], [], []
        self.disable()
        try:
            for uid, nm in wanted.items():
                u = users.get(str(uid))
                if not u:
                    missing.append(uid)
                    continue
                limit = 8 if getattr(self, "user_packet_size", 72) == 28 else 23
                nm = (nm or "").strip()
                if u.get("raw_name", "") == nm.encode("utf-8", "ignore")[:limit].decode("utf-8", "ignore"):
                    same.append(uid)
                    continue
                self.set_user(u, name=nm)
                updated.append(uid)
            if updated:
                self._cmd(CMD_REFRESHDATA)
        finally:
            try:
                self.enable()
            except Exception:
                pass
        return updated, missing, same

    def get_attendance(self, users=None):
        sizes = self.read_sizes()
        data = self._read_with_buffer(CMD_ATTLOG_RRQ)
        if len(data) <= 4:
            return []
        total = struct.unpack("<I", data[:4])[0]
        data = data[4:4 + total]
        n = sizes.get("records") or 0
        if n:
            rsize = total // n
        else:
            rsize = 40 if total % 40 == 0 else (16 if total % 16 == 0 else 8)
        out = []
        if rsize == 8:
            uid_map = {}
            if users is None:
                users = self.get_users()
            for u in users:
                uid_map[u["uid"]] = u["user_id"]
            while len(data) >= 8:
                uid, status, ts, punch = struct.unpack("<HB4sB", data[:8])
                out.append({"user_id": uid_map.get(uid, str(uid)),
                            "ts": decode_time(struct.unpack("<I", ts)[0]),
                            "verify": status, "state": punch})
                data = data[8:]
        elif rsize == 16:
            while len(data) >= 16:
                user_id, ts, status, punch, _r, _w = struct.unpack("<I4sBB2sI", data[:16])
                out.append({"user_id": str(user_id), "ts": decode_time(struct.unpack("<I", ts)[0]),
                            "verify": status, "state": punch})
                data = data[16:]
        else:
            while len(data) >= 40:
                uid, user_id, status, ts, punch = struct.unpack("<H24sB4sB8x", data[:40])
                out.append({"user_id": _clean(user_id) or str(uid),
                            "ts": decode_time(struct.unpack("<I", ts)[0]),
                            "verify": status, "state": punch})
                data = data[40:]
        return out


def test_device(ip, port=4370, password=0):
    with ZKClient(ip, port, password, timeout=8) as z:
        sizes = z.read_sizes()
        t = z.get_time()
        proto = "UDP" if z.udp else "TCP"
        try:
            serial = z.get_serial()
        except Exception:
            serial = ""
    return {"ok": True, "protocol": proto, "serial": serial, "users": sizes["users"], "records": sizes["records"],
            "device_time": t.strftime("%Y-%m-%d %H:%M:%S") if t else None}


# ---------------------------------------------------------------- discovery
def _probe(ip, port, password, timeout):
    try:
        z = ZKClient(ip, port, password, timeout=timeout)
        z.connect()
        try:
            info = {"ip": ip, "port": port, "protocol": "UDP" if z.udp else "TCP", "serial": "", "users": None}
            try:
                info["serial"] = z.get_serial()
                info["users"] = z.read_sizes().get("users")
            except Exception:
                pass
            return info
        finally:
            z.disconnect()
    except ZKError as e:
        if "Comm Key" in str(e):
            return {"ip": ip, "port": port, "protocol": "?", "serial": "", "users": None, "needs_key": True}
    except Exception:
        pass
    return None


def discover(prefix, port=4370, password=0, timeout=1.0, workers=64, only_serial=None):
    """Scan prefix.1 - prefix.254 (e.g. prefix='192.168.10') for eSSL / ZKTeco machines.
    If only_serial is given, stops as soon as that machine is found."""
    import concurrent.futures as cf
    prefix = ".".join(str(prefix).split(".")[:3])
    found = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_probe, f"{prefix}.{i}", port, password, timeout): i for i in range(1, 255)}
        for f in cf.as_completed(futs):
            r = f.result()
            if r:
                found.append(r)
                if only_serial and r.get("serial") == only_serial:
                    for other in futs:
                        other.cancel()
                    break
    found.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
    return found


def find_moved(old_ip, serial, port=4370, password=0):
    """The machine with this serial no longer answers at old_ip: look for it on the same network."""
    if not serial:
        return None
    for r in discover(old_ip.rsplit(".", 1)[0], port, password, only_serial=serial):
        if r.get("serial") == serial:
            return r["ip"]
    return None
