"""eSSL / ZKTeco user photos over the LAN, using eSSL's own Windows SDK (zkemkeeper.dll).

The plain machine protocol used for attendance has no command for photos, so on Windows we ask the official
SDK (installed with eTimeTrackLite / eSSL "Standalone SDK") to copy the photo files off the machine.
Works on Windows only, and only when the SDK is registered for the same bitness (32/64-bit) as this Python.
Machines in ADMS (cloud) mode send photos by themselves; Hikvision photos are read directly — this file is not
needed for those.
"""
import os
import sys
import glob
import shutil
import tempfile

NOT_WINDOWS = "eSSL photo download over LAN works on the Windows PC only."
NO_SDK = ("eSSL SDK (zkemkeeper) not found on this PC. Install eTimeTrackLite or the eSSL 'Standalone SDK' "
          "({bits}-bit) on this computer, or switch the machine to ADMS (cloud server) mode — then photos come by themselves.")


def _sdk():
    """Create the zkemkeeper COM object with whichever COM library is present."""
    errors = []
    try:
        import win32com.client  # pywin32
        return win32com.client.Dispatch("zkemkeeper.ZKEM.1"), "pywin32"
    except Exception as e:
        errors.append(str(e))
    try:
        import comtypes.client
        return comtypes.client.CreateObject("zkemkeeper.ZKEM.1"), "comtypes"
    except Exception as e:
        errors.append(str(e))
    raise RuntimeError(NO_SDK.format(bits=64 if sys.maxsize > 2 ** 32 else 32) + f" [{'; '.join(errors)[:160]}]")


def _ok(r):
    return bool(r[0] if isinstance(r, (tuple, list)) else r)


def _jpeg(b):
    return b[:3] == b"\xff\xd8\xff" and len(b) > 600


def fetch_photos(ip, port, comm_key, pins, limit=500):
    """Returns (photos: {pin: jpeg bytes}, message)."""
    if os.name != "nt":
        return {}, NOT_WINDOWS
    try:
        zk, lib = _sdk()
    except RuntimeError as e:
        return {}, str(e)
    M = 1
    try:
        if comm_key:
            try:
                zk.SetCommPassword(int(comm_key))
            except Exception:
                pass
        if not _ok(zk.Connect_Net(ip, int(port or 4370))):
            return {}, f"eSSL SDK could not connect to {ip}:{port}"
    except Exception as e:
        return {}, f"eSSL SDK error: {e}"
    out, tmp = {}, tempfile.mkdtemp(prefix="hayat_photos_")
    tried = 0
    try:
        for pin in [str(p) for p in pins][:limit]:
            tried += 1
            got = None
            for fname in (f"{pin}.jpg", f"photo/{pin}.jpg", f"/mnt/mtdblock/photo/{pin}.jpg", f"/mnt/sdcard/photo/{pin}.jpg"):
                dest = os.path.join(tmp, f"{pin}.jpg")
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                    if _ok(zk.ReadFile(M, fname, tmp + os.sep)) or os.path.exists(dest):
                        for cand in [dest] + glob.glob(os.path.join(tmp, f"*{pin}*.jpg")):
                            if os.path.exists(cand):
                                b = open(cand, "rb").read()
                                if _jpeg(b):
                                    got = b
                                    break
                except Exception:
                    pass
                if got:
                    break
            if not got and hasattr(zk, "DownloadUserPhoto"):
                try:
                    zk.DownloadUserPhoto(M, f"{pin}.jpg", tmp + os.sep)
                    dest = os.path.join(tmp, f"{pin}.jpg")
                    if os.path.exists(dest) and _jpeg(open(dest, "rb").read()):
                        got = open(dest, "rb").read()
                except Exception:
                    pass
            if got:
                out[pin] = got
            elif tried >= 8 and not out:
                break   # first 8 users had no photo file: this machine does not store user photos this way
    finally:
        try:
            zk.Disconnect()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    if out:
        return out, f"{len(out)} photo(s) read with the eSSL SDK ({lib})"
    return {}, ("The machine did not give user photos through the eSSL SDK (it may not store photos, or keeps "
                "face data only). Use ADMS (cloud server) mode on this machine for photos.")
