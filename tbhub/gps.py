"""GNSS sources (gpsd or direct serial) and coordinate formatting (DMS, UTM, MGRS)."""

import errno
import glob
import json
import os
import select
import math
import socket
import threading
import time
from datetime import datetime, timezone

from . import gps_serial

GNSS_NAMES = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS",
              6: "GLONASS", 7: "NavIC"}
FIX_MODES = {0: "Unknown", 1: "No fix", 2: "2D fix", 3: "3D fix"}


class GpsState:
    def __init__(self):
        self.connected = False
        self.error = None
        self.source = None      # e.g. 'gpsd 127.0.0.1:2947' or 'serial /dev/gps0 @ 9600'
        self.device = None      # last DEVICE message
        self.tpv = {}           # last TPV message
        self.sky = {}           # merged SKY (satellite list kept across partial updates)
        self.updated = 0.0

    @property
    def mode(self):
        return self.tpv.get("mode", 0)

    @property
    def has_fix(self):
        return self.mode >= 2 and "lat" in self.tpv and "lon" in self.tpv

    def satellites(self):
        return self.sky.get("satellites", [])

    def sat_counts(self):
        sats = self.satellites()
        used = self.sky.get("uSat", sum(1 for s in sats if s.get("used")))
        seen = self.sky.get("nSat", len(sats))
        return used, seen

    def receiver_time(self):
        t = self.tpv.get("time") or self.sky.get("time")
        if not t:
            return None
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None

    def time_warning(self):
        """Explain a receiver clock that disagrees with system time by > 1 day (week rollover)."""
        rt = self.receiver_time()
        if rt is None:
            return None
        delta = abs((datetime.now(timezone.utc) - rt).total_seconds())
        if delta > 86400:
            return (f"GPS date {rt:%Y-%m-%d} is {delta / 86400:.0f} days off system time - a "
                    "GPS week-number rollover error. Position is unaffected; don't use this "
                    "source as a time reference.")
        return None

    def to_dict(self):
        return {"source": self.source, "connected": self.connected, "error": self.error, "device": self.device,
                "tpv": self.tpv, "sky": self.sky}


DEFAULT_SOURCE = {
    "gps_source": "auto",        # auto | gpsd | serial
    "gpsd_host": "127.0.0.1",
    "gpsd_port": 2947,
    "gps_device": "/dev/gps0",
    "gps_baud": 9600,
}
SOURCES = ("auto", "gpsd", "serial")


def source_config(cfg=None):
    out = dict(DEFAULT_SOURCE)
    out.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT_SOURCE and v is not None})
    out["gpsd_port"] = int(out["gpsd_port"])
    out["gps_baud"] = int(out["gps_baud"])
    return out


def gpsd_running():
    """True if a gpsd process exists (checked via /proc, so it never socket-activates gpsd)."""
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm) as fh:
                if fh.read().strip() == "gpsd":
                    return True
        except OSError:
            pass
    return False


def gpsd_devices(host, port, timeout=2.0):
    """Device paths gpsd currently has, or None if gpsd isn't reachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"?DEVICES;\n")
            buf, end = b"", time.monotonic() + timeout
            while time.monotonic() < end:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "DEVICES":
                        return [d.get("path") for d in msg.get("devices", []) if d.get("path")]
    except OSError:
        return None
    return []


def serial_conflict(cfg):
    """Explain why reading cfg['gps_device'] directly would fight gpsd, or None if it's safe."""
    if not gpsd_running():
        return None
    dev = cfg["gps_device"]
    devs = gpsd_devices(cfg["gpsd_host"], cfg["gpsd_port"])
    stop = "sudo systemctl stop gpsd.socket gpsd"
    if devs is None:
        return (f"gpsd is running but not answering at {cfg['gpsd_host']}:{cfg['gpsd_port']}; "
                f"it may be holding {dev}. Stop it first: {stop}")
    real = os.path.realpath(dev)
    if real in {os.path.realpath(d) for d in devs}:
        return (f"gpsd is already reading {dev}. Two readers corrupt each other's data - use the "
                f"gpsd source, or stop it first: {stop}")
    return None


class GpsSource(threading.Thread):
    """Background GNSS reader. Source 'gpsd', 'serial', or 'auto' (gpsd, else direct serial).

    Calls `on_update(state)` (from this thread) after every update.
    """

    PROBE_INTERVAL = 30.0   # auto mode: how often serial mode checks whether gpsd came back
    NO_DATA_TIMEOUT = 5.0

    def __init__(self, cfg=None, on_update=None):
        super().__init__(daemon=True)
        self.cfg = source_config(cfg)
        self.on_update = on_update
        self.state = GpsState()
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def _notify(self):
        self.state.updated = time.time()
        if self.on_update:
            self.on_update(self.state)

    def _fail(self, msg):
        self.state.connected, self.state.error = False, msg
        self._notify()

    def run(self):
        backoff = 1
        mode = self.cfg["gps_source"]
        while not self._stop_evt.is_set():
            if mode in ("gpsd", "auto"):
                ok, why = self._run_gpsd(require_device=(mode == "auto"))
                if ok:
                    backoff = 1
                    continue
                if mode == "gpsd":
                    self._fail(why)
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 15)
                    continue
                if not os.path.exists(self.cfg["gps_device"]):
                    self._fail(f"{why}; serial fallback {self.cfg['gps_device']} not found")
                    self._stop_evt.wait(5)
                    continue
            conflict = serial_conflict(self.cfg)
            if conflict:
                self.state.source = f"serial {self.cfg['gps_device']}"
                self._fail(conflict)
                self._stop_evt.wait(5)
                continue
            self._run_serial(probe_gpsd=(mode == "auto"))
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, 15)

    # ------------------------------------------------------------------ gpsd
    def _run_gpsd(self, require_device):
        """Stream from gpsd. Returns (had_session, reason_if_not)."""
        host, port = self.cfg["gpsd_host"], self.cfg["gpsd_port"]
        self.state.source = f"gpsd {host}:{port}"
        try:
            s = socket.create_connection((host, port), timeout=3)
        except OSError as e:
            return False, f"gpsd {host}:{port} unreachable ({e.strerror or e})"
        with s:
            self.state = GpsState()
            self.state.source = f"gpsd {host}:{port}"
            s.sendall(b'?WATCH={"enable":true,"json":true};\n')
            s.settimeout(1)
            buf, got_device, started = b"", False, time.monotonic()
            self.state.connected = True
            self._notify()
            try:
                while not self._stop_evt.is_set():
                    if require_device and not got_device and time.monotonic() - started > 3:
                        return False, f"gpsd {host}:{port} has no receiver attached"
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        self._fail("gpsd closed the connection")
                        return True, None
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        got_device |= self._process(line)
            except OSError as e:
                self._fail(str(e))
            return True, None

    def _process(self, line):
        """Handle one gpsd JSON line; returns True if it shows gpsd has a device."""
        try:
            msg = json.loads(line)
        except ValueError:
            return False
        cls = msg.get("class")
        has_dev = False
        if cls == "TPV":
            self.state.tpv = msg
            has_dev = True
        elif cls == "SKY":
            merged = dict(self.state.sky)
            merged.update(msg)
            self.state.sky = merged
            has_dev = True
        elif cls == "DEVICE":
            self.state.device = msg
            has_dev = True
        elif cls == "DEVICES" and msg.get("devices"):
            self.state.device = msg["devices"][0]
            has_dev = True
        else:
            return False
        self._notify()
        return has_dev

    # ------------------------------------------------------------------ serial
    def _run_serial(self, probe_gpsd):
        dev, baud = self.cfg["gps_device"], self.cfg["gps_baud"]
        self.state = GpsState()
        self.state.source = f"serial {dev} @ {baud}"
        try:
            fd = gps_serial.open_serial(dev, baud)
        except OSError as e:
            hint = " (add yourself to the 'dialout' group)" if e.errno == errno.EACCES else ""
            self._fail(f"cannot open {dev}: {e.strerror}{hint}")
            return
        dec = gps_serial.Decoder()
        last_data = opened = time.monotonic()
        next_probe = opened + self.PROBE_INTERVAL
        self.state.connected = True
        self.state.device = {"class": "DEVICE", "path": dev, "driver": "serial", "bps": baud}
        self._notify()
        try:
            while not self._stop_evt.is_set():
                r, _, _ = select.select([fd], [], [], 1.0)
                now = time.monotonic()
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except BlockingIOError:
                        data = b""
                    if not data and r:
                        raise OSError(errno.ENODEV, "device disconnected")
                    if dec.feed(data):
                        last_data = now
                        self.state.tpv, self.state.sky = dict(dec.tpv), dict(dec.sky)
                        self.state.device["driver"] = "+".join(sorted(dec.protocols, reverse=True))
                        self.state.error = None
                        self._notify()
                if now - last_data > self.NO_DATA_TIMEOUT:
                    self.state.error = (f"no NMEA/UBX data from {dev} for "
                                        f"{now - last_data:.0f}s - wrong baud rate?")
                    self._notify()
                if probe_gpsd and now >= next_probe:
                    next_probe = now + self.PROBE_INTERVAL
                    if gpsd_running() and gpsd_devices(self.cfg["gpsd_host"],
                                                       self.cfg["gpsd_port"], 1.0):
                        return  # gpsd is back with a receiver: auto mode prefers it
        except OSError as e:
            self._fail(f"{dev}: {e.strerror or e}")
        finally:
            os.close(fd)


def snapshot(cfg=None, timeout=5.0, need_fix=True):
    """Collect a single GpsState (waits up to `timeout` s for a fix + satellites)."""
    done = threading.Event()

    def cb(st):
        if st.tpv and st.satellites() and (st.has_fix or not need_fix):
            done.set()

    c = GpsSource(cfg, on_update=cb)
    c.start()
    done.wait(timeout)
    c.stop()
    return c.state


# --------------------------------------------------------------------------- formatting

def fmt_accuracy(st):
    t, sky = st.tpv, st.sky
    if "eph" in t:
        return f"±{t['eph']:.1f} m H" + (f"  ±{t['epv']:.1f} m V" if "epv" in t else "")
    for k in ("hdop", "pdop"):
        if sky.get(k) is not None:
            return f"{k.upper()} {sky[k]:.2f} (no metre estimate from this source)"
    return "—"


def fmt_dms(value, is_lat):
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d}°{m:02d}'{s:05.2f}\"{hemi}"


def fmt_ddm(value, is_lat):
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    v = abs(value)
    d = int(v)
    return f"{d}°{(v - d) * 60:06.3f}'{hemi}"


_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996


def utm_zone(lat, lon):
    zone = int((lon + 180) // 6) + 1
    if zone > 60:
        zone = 60
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    return zone


def latlon_to_utm(lat, lon):
    """WGS84 lat/lon -> (zone, hemisphere 'N'/'S', easting, northing)."""
    zone = utm_zone(lat, lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - lon0)
    e4, e6 = _E2 ** 2, _E2 ** 3
    m = _A * ((1 - _E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
              - (3 * _E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
              + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
              - (35 * e6 / 3072) * math.sin(6 * phi))
    easting = _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                         + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000.0
    northing = _K0 * (m + n * math.tan(phi) * (a * a / 2
                      + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                      + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    if lat < 0:
        northing += 10000000.0
    return zone, ("N" if lat >= 0 else "S"), easting, northing


_BANDS = "CDEFGHJKLMNPQRSTUVWX"
_COL_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"


def latlon_to_mgrs(lat, lon, precision=5):
    """WGS84 lat/lon -> MGRS string like '17R NK 66123 31987' (UTM area only, |lat| < 84)."""
    if not -80 <= lat < 84:
        return None
    zone, _, e, n = latlon_to_utm(lat, lon)
    band = _BANDS[min(int((lat + 80) // 8), len(_BANDS) - 1)]
    col = _COL_SETS[(zone - 1) % 3][int(e // 100000) - 1]
    row_offset = 5 if zone % 2 == 0 else 0
    row = _ROW_LETTERS[(int(n // 100000) + row_offset) % 20]
    div = 10 ** (5 - precision)
    ee = int(e % 100000) // div
    nn = int(n % 100000) // div
    return f"{zone}{band} {col}{row} {ee:0{precision}d} {nn:0{precision}d}"


def fmt_utm(lat, lon):
    zone, hemi, e, n = latlon_to_utm(lat, lon)
    return f"{zone}{hemi} {e:.0f}E {n:.0f}N"
