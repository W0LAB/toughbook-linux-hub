"""Direct serial GNSS reading: a stream decoder for u-blox UBX binary + NMEA 0183.

The decoder turns receiver output into gpsd-shaped `tpv` / `sky` dicts so the rest of
tbhub doesn't care where the data came from. UBX NAV-PVT / NAV-SAT are preferred when
present (they carry real accuracy estimates); NMEA is used otherwise.
"""

import glob
import os
import struct
import termios
import tty
from datetime import datetime, timezone

# --------------------------------------------------------------------------- serial port

BAUDS = [4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800]


def list_serial_devices():
    """Likely GNSS serial devices, stable names first."""
    seen, out = set(), []
    for pat in ("/dev/gps*", "/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*",
                "/dev/rfcomm*", "/dev/ttyS[0-9]"):
        for p in sorted(glob.glob(pat)):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(p)
    return out


def open_serial(path, baud=9600):
    """Open a tty read-only in raw mode at `baud`. Returns an fd (non-blocking)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        if os.isatty(fd):
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{int(baud)}", termios.B9600)
            attrs[4] = attrs[5] = speed
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        os.close(fd)
        raise
    return fd


# --------------------------------------------------------------------------- helpers

# gnssId numbering (same as u-blox and gpsd): 0 GPS 1 SBAS 2 Galileo 3 BeiDou 4 IMES 5 QZSS 6 GLONASS 7 NavIC
_TALKER_GNSS = {"GP": 0, "GL": 6, "GA": 2, "GB": 3, "BD": 3, "GQ": 5, "QZ": 5, "GI": 7}
_NMEA_SYSID = {1: 0, 2: 6, 3: 2, 4: 3, 5: 5, 6: 7}  # NMEA 4.10 GSA/GSV system id -> gnssId


def _normalize(gnss, prn):
    """(gnss hint or None, NMEA PRN) -> (gnssId, svid). Handles u-blox extended numbering."""
    if gnss is None:
        if 1 <= prn <= 32:
            gnss = 0
        elif 33 <= prn <= 64 or 120 <= prn <= 158:
            gnss = 1
        elif 65 <= prn <= 96:
            gnss = 6
        elif 193 <= prn <= 202:
            gnss = 5
        elif 201 <= prn <= 237 or 401 <= prn <= 463:
            gnss = 3
        elif 301 <= prn <= 336:
            gnss = 2
        else:
            gnss = 0
    sv = prn
    if gnss == 6 and prn > 64:
        sv = prn - 64
    elif gnss == 1 and 33 <= prn <= 64:
        sv = prn + 87
    elif gnss == 5 and prn > 192:
        sv = prn - 192
    elif gnss == 3 and prn > 400:
        sv = prn - 400
    elif gnss == 3 and prn > 200:
        sv = prn - 200
    elif gnss == 2 and prn > 300:
        sv = prn - 300
    return gnss, sv


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _latlon(val, hemi):
    if not val or not hemi:
        return None
    try:
        dot = val.index(".")
        deg = float(val[:dot - 2])
        minutes = float(val[dot - 2:])
    except ValueError:
        return None
    v = deg + minutes / 60
    return -v if hemi in "SW" else v


def nmea_checksum_ok(line):
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].rpartition("*")
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        return x == int(cs[:2], 16)
    except ValueError:
        return False


# --------------------------------------------------------------------------- decoder

class Decoder:
    MAX_NMEA = 128

    def __init__(self):
        self.buf = b""
        self.tpv = {"class": "TPV", "mode": 0}
        self.sky = {"class": "SKY", "satellites": []}
        self.protocols = set()
        self.ubx_pvt = False   # once NAV-PVT is seen, NMEA position is ignored
        self.ubx_sat = False   # once NAV-SAT is seen, NMEA satellites are ignored
        # NMEA accumulation
        self._gsv = {}         # (talker, signal) -> {"total", "sats"}
        self._gsv_done = {}    # (talker, signal) -> list of sats
        self._used_pending = set()
        self._used = set()
        self._epoch = None
        self._date = None

    # ---------------------------------------------------------------- framing
    def feed(self, data):
        """Feed raw bytes. Returns True if tpv/sky changed."""
        self.buf += data
        changed = False
        while True:
            iu = self.buf.find(b"\xb5\x62")
            inm = self.buf.find(b"$")
            cands = [i for i in (iu, inm) if i >= 0]
            if not cands:
                self.buf = self.buf[-1:] if self.buf.endswith(b"\xb5") else b""
                return changed
            start = min(cands)
            self.buf = self.buf[start:]
            if start == iu:
                if len(self.buf) < 6:
                    return changed
                cls, mid, ln = struct.unpack_from("<BBH", self.buf, 2)
                if ln > 4096:
                    self.buf = self.buf[2:]
                    continue
                if len(self.buf) < 8 + ln:
                    return changed
                frame = self.buf[2:6 + ln]
                ck_a = ck_b = 0
                for b in frame:
                    ck_a = (ck_a + b) & 0xFF
                    ck_b = (ck_b + ck_a) & 0xFF
                if bytes((ck_a, ck_b)) != self.buf[6 + ln:8 + ln]:
                    self.buf = self.buf[2:]
                    continue
                self.buf = self.buf[8 + ln:]
                self.protocols.add("UBX")
                changed |= self._ubx(cls, mid, frame[4:])
            else:
                nl = self.buf.find(b"\n", 0, self.MAX_NMEA)
                if nl < 0:
                    if len(self.buf) >= self.MAX_NMEA:
                        self.buf = self.buf[1:]
                        continue
                    return changed
                raw, self.buf = self.buf[:nl], self.buf[nl + 1:]
                try:
                    line = raw.decode("ascii").strip()
                except UnicodeDecodeError:
                    self.buf = raw[1:] + b"\n" + self.buf  # resync after the '$'
                    continue
                if nmea_checksum_ok(line):
                    self.protocols.add("NMEA")
                    changed |= self._nmea(line)

    # ---------------------------------------------------------------- UBX
    def _ubx(self, cls, mid, p):
        if cls == 0x01 and mid == 0x07 and len(p) >= 92:      # NAV-PVT
            (year, month, day, hour, minute, sec, valid) = struct.unpack_from("<HBBBBBB", p, 4)
            nano = struct.unpack_from("<i", p, 16)[0]
            fix_type, flags, _, num_sv = struct.unpack_from("<BBBB", p, 20)
            lon, lat, height, hmsl = struct.unpack_from("<iiii", p, 24)
            hacc, vacc = struct.unpack_from("<II", p, 40)
            vel_n, vel_e, vel_d, gspeed, head = struct.unpack_from("<iiiii", p, 48)
            pdop = struct.unpack_from("<H", p, 76)[0]
            ok = bool(flags & 0x01)
            mode = {2: 2, 3: 3, 4: 3}.get(fix_type, 1) if ok else 1
            tpv = {"class": "TPV", "mode": mode}
            if valid & 0x03 == 0x03:
                try:
                    t = datetime(year, month, day, hour, minute, sec, tzinfo=timezone.utc)
                    tpv["time"] = t.strftime("%Y-%m-%dT%H:%M:%S") + \
                        f".{max(0, nano) // 1_000_000:03d}Z"
                except ValueError:
                    pass
            if mode >= 2:
                tpv.update(lat=lat * 1e-7, lon=lon * 1e-7, eph=hacc / 1000.0,
                           speed=gspeed / 1000.0, track=head * 1e-5,
                           climb=-vel_d / 1000.0)
                if mode == 3:
                    tpv.update(altMSL=hmsl / 1000.0, altHAE=height / 1000.0, epv=vacc / 1000.0)
            self.tpv = tpv
            self.sky["pdop"] = pdop / 100.0
            self.sky["uSat"] = num_sv
            self.ubx_pvt = True
            return True
        if cls == 0x01 and mid == 0x35 and len(p) >= 8:       # NAV-SAT
            n = p[5]
            sats = []
            for k in range(n):
                off = 8 + 12 * k
                if off + 12 > len(p):
                    break
                gnss, sv, cno, elev, azim, _pr, fl = struct.unpack_from("<BBBbhhI", p, off)
                sats.append({"gnssid": gnss, "svid": sv, "PRN": sv, "ss": float(cno),
                             "el": float(elev), "az": float(azim), "used": bool(fl & 0x08)})
            self.sky["satellites"] = sats
            self.sky["nSat"] = len(sats)
            self.sky["uSat"] = sum(1 for s in sats if s["used"])
            self.ubx_sat = True
            return True
        return False

    # ---------------------------------------------------------------- NMEA
    def _nmea(self, line):
        body = line[1:line.rindex("*")]
        f = body.split(",")
        talker, kind = f[0][:2], f[0][2:]
        handler = getattr(self, f"_nmea_{kind.lower()}", None)
        return handler(talker, f) if handler else False

    def _new_epoch(self, t):
        if t and t != self._epoch:
            self._epoch = t
            self._used, self._used_pending = self._used_pending, set()
            self._apply_used()

    def _time_iso(self, hhmmss):
        if not self._date or not hhmmss or len(hhmmss) < 6:
            return None
        d, m, y = int(self._date[0:2]), int(self._date[2:4]), 2000 + int(self._date[4:6])
        try:
            return f"{y:04d}-{m:02d}-{d:02d}T{hhmmss[0:2]}:{hhmmss[2:4]}:{float(hhmmss[4:]):06.3f}Z"
        except ValueError:
            return None

    def _nmea_rmc(self, talker, f):
        if len(f) < 10:
            return False
        if f[9] and len(f[9]) == 6:
            self._date = f[9]
        self._new_epoch(f[1])
        if self.ubx_pvt:
            return False
        tpv = self.tpv
        t = self._time_iso(f[1])
        if t:
            tpv["time"] = t
        if f[2] != "A":
            tpv["mode"] = 1
            return True
        lat, lon = _latlon(f[3], f[4]), _latlon(f[5], f[6])
        if lat is not None and lon is not None:
            tpv.update(lat=lat, lon=lon)
            tpv["mode"] = max(tpv.get("mode", 0), 2)
        spd, trk = _f(f[7]), _f(f[8])
        if spd is not None:
            tpv["speed"] = spd * 0.514444
        if trk is not None:
            tpv["track"] = trk
        return True

    def _pos_fix(self, f, lat_i, qual_ok, alt_i, hdop_i, nsv_i):
        if self.ubx_pvt:
            return False
        tpv = self.tpv
        if not qual_ok:
            tpv["mode"] = 1
            for k in ("lat", "lon", "altMSL"):
                tpv.pop(k, None)
            return True
        lat, lon = _latlon(f[lat_i], f[lat_i + 1]), _latlon(f[lat_i + 2], f[lat_i + 3])
        if lat is None or lon is None:
            return False
        tpv.update(lat=lat, lon=lon)
        alt = _f(f[alt_i]) if len(f) > alt_i else None
        if alt is not None:
            tpv["altMSL"] = alt
        if tpv.get("mode", 0) < 2:
            tpv["mode"] = 3 if alt is not None else 2
        hdop = _f(f[hdop_i]) if len(f) > hdop_i else None
        if hdop is not None:
            self.sky["hdop"] = hdop
        nsv = _i(f[nsv_i]) if len(f) > nsv_i else None
        if nsv is not None and not self.ubx_sat:
            self.sky["uSat"] = nsv
        return True

    def _nmea_gga(self, talker, f):
        if len(f) < 10:
            return False
        self._new_epoch(f[1])
        return self._pos_fix(f, 2, _i(f[6]) not in (None, 0), 9, 8, 7)

    def _nmea_gns(self, talker, f):
        if len(f) < 10:
            return False
        self._new_epoch(f[1])
        return self._pos_fix(f, 2, bool(f[6]) and set(f[6]) != {"N"}, 9, 8, 7)

    def _nmea_gsa(self, talker, f):
        if len(f) < 18:
            return False
        sysid = _i(f[18]) if len(f) > 18 else None
        hint = _NMEA_SYSID.get(sysid) if sysid else _TALKER_GNSS.get(talker)
        for prn in f[3:15]:
            p = _i(prn)
            if p:
                self._used_pending.add(_normalize(hint, p))
        if not self.ubx_pvt:
            fix = _i(f[2])
            if fix:
                self.tpv["mode"] = fix
                if fix < 2:
                    for k in ("lat", "lon", "altMSL"):
                        self.tpv.pop(k, None)
            for k, i in (("pdop", 15), ("hdop", 16), ("vdop", 17)):
                v = _f(f[i])
                if v is not None:
                    self.sky[k] = v
        return False  # committed on the next epoch

    def _nmea_gsv(self, talker, f):
        if self.ubx_sat or len(f) < 4:
            return False
        total, num = _i(f[1]), _i(f[2])
        if not total or not num:
            return False
        n_fields = len(f) - 4
        signal = f[-1] if n_fields % 4 == 1 else ""
        key = (talker, signal)
        hint = _TALKER_GNSS.get(talker)
        if num == 1:
            self._gsv[key] = []
        acc = self._gsv.setdefault(key, [])
        for k in range(4, 4 + (n_fields // 4) * 4, 4):
            prn = _i(f[k])
            if not prn:
                continue
            gnss, sv = _normalize(hint, prn)
            acc.append({"gnssid": gnss, "svid": sv, "PRN": prn, "el": _f(f[k + 1]) or 0.0,
                        "az": _f(f[k + 2]) or 0.0, "ss": _f(f[k + 3]) or 0.0})
        if num == total:
            self._gsv_done[key] = self._gsv.pop(key, [])
            self._apply_used()
            return True
        return False

    def _apply_used(self):
        if self.ubx_sat:
            return
        merged = {}
        for sats in self._gsv_done.values():
            for s in sats:
                k = (s["gnssid"], s["svid"])
                if k not in merged or s["ss"] > merged[k]["ss"]:  # keep strongest signal
                    merged[k] = dict(s)
        for k, s in merged.items():
            s["used"] = k in self._used
        sats = list(merged.values())
        self.sky["satellites"] = sats
        self.sky["nSat"] = len(sats)
        if self._used:
            self.sky["uSat"] = sum(1 for s in sats if s["used"]) or len(self._used)
