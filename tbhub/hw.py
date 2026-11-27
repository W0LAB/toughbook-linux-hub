"""Low-level hardware access: backlight, Panasonic ACPI, battery, rfkill, audio, gamma."""

import os
import shutil
import struct
import subprocess
from pathlib import Path

PANASONIC_ACPI = Path("/sys/bus/acpi/devices/MAT0019:00")
BACKLIGHT_ROOT = Path("/sys/class/backlight")
POWER_ROOT = Path("/sys/class/power_supply")
RFKILL_ROOT = Path("/sys/class/rfkill")
DMI_ROOT = Path("/sys/class/dmi/id")


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def write(path, value):
    Path(path).write_text(str(value))


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


# --------------------------------------------------------------------------- backlight

class Backlight:
    """Kernel backlight device. Prefers 'raw' (GPU) over 'platform'/'firmware'."""

    _PREF = {"raw": 0, "platform": 1, "firmware": 2}

    def __init__(self, name=None):
        devs = [p for p in BACKLIGHT_ROOT.iterdir()] if BACKLIGHT_ROOT.exists() else []
        if name:
            devs = [d for d in devs if d.name == name]
        devs.sort(key=lambda d: self._PREF.get(read(d / "type"), 9))
        self.path = devs[0] if devs else None

    @property
    def available(self):
        return self.path is not None

    @property
    def name(self):
        return self.path.name if self.path else None

    @property
    def max(self):
        return read_int(self.path / "max_brightness") or 1

    @property
    def raw(self):
        return read_int(self.path / "brightness") or 0

    @raw.setter
    def raw(self, value):
        write(self.path / "brightness", max(0, min(int(value), self.max)))

    @property
    def percent(self):
        return 100.0 * self.raw / self.max

    def set_percent(self, pct, floor=1):
        """Set brightness in percent; never goes below raw `floor` (0 can blank some panels)."""
        self.raw = max(floor, round(self.max * max(0.0, min(pct, 100.0)) / 100.0))

    @property
    def writable(self):
        return self.available and os.access(self.path / "brightness", os.W_OK)


# --------------------------------------------------------------------------- Panasonic ACPI

PANASONIC_ATTRS = {
    # name: (label, description)
    "sticky_key": ("Sticky Fn key", "Fn acts as a latching key (press, release, then press F-key)"),
    "ac_brightness": ("BIOS brightness on AC", "Firmware brightness preset when on AC power"),
    "dc_brightness": ("BIOS brightness on battery", "Firmware brightness preset when on battery"),
    "current_brightness": ("Firmware brightness", "Firmware-side brightness value"),
    "mute": ("Firmware mute", "Hardware/firmware audio mute flag"),
    "eco_mode": ("ECO battery mode", "Limit charge to extend battery life"),
    "cdpower": ("Optical drive power", "Power to the optical drive bay"),
    "lcdtype": ("LCD type", "Panel type reported by firmware"),
    "numbatt": ("Battery slots", "Number of battery bays"),
}


class Panasonic:
    path = PANASONIC_ACPI

    @property
    def available(self):
        return self.path.exists()

    def attrs(self):
        """Return {name: {value, writable, label, desc}} for attributes that actually read."""
        out = {}
        for name, (label, desc) in PANASONIC_ATTRS.items():
            p = self.path / name
            if not p.exists():
                continue
            v = read(p)
            if v is None:  # e.g. EIO: firmware doesn't implement it on this model
                continue
            out[name] = {"value": v, "writable": os.access(p, os.W_OK),
                         "label": label, "desc": desc}
        return out

    def get(self, name):
        return read(self.path / name)

    def set(self, name, value):
        write(self.path / name, value)


# --------------------------------------------------------------------------- power

def power_status():
    ac, batteries = None, []
    if not POWER_ROOT.exists():
        return {"ac_online": None, "batteries": []}
    for d in sorted(POWER_ROOT.iterdir()):
        t = read(d / "type")
        if t == "Mains":
            ac = read(d / "online") == "1"
        elif t == "Battery" and read(d / "scope") != "Device":  # skip mice/keyboards
            def num(n, scale=1e6):
                v = read_int(d / n)
                return None if v is None else v / scale
            e_now = num("energy_now") or num("charge_now")
            e_full = num("energy_full") or num("charge_full")
            e_design = num("energy_full_design") or num("charge_full_design")
            p_now = num("power_now")
            status = read(d / "status")
            hours = None
            if p_now and p_now > 0.1 and e_now is not None and e_full is not None:
                if status == "Discharging":
                    hours = e_now / p_now
                elif status == "Charging":
                    hours = (e_full - e_now) / p_now
            batteries.append({
                "name": d.name,
                "model": read(d / "model_name"),
                "manufacturer": read(d / "manufacturer"),
                "technology": read(d / "technology"),
                "capacity": read_int(d / "capacity"),
                "status": status,
                "power_w": p_now,
                "energy_wh": e_now,
                "full_wh": e_full,
                "design_wh": e_design,
                "health": (100.0 * e_full / e_design) if e_full and e_design else None,
                "cycles": read_int(d / "cycle_count"),
                "voltage": num("voltage_now"),
                "hours": hours,
            })
    return {"ac_online": ac, "batteries": batteries}


# --------------------------------------------------------------------------- rfkill

RFKILL_TYPES = {0: "all", 1: "wlan", 2: "bluetooth", 3: "uwb", 4: "wimax", 5: "wwan",
                6: "gps", 7: "fm", 8: "nfc"}
_RFKILL_TYPE_IDS = {v: k for k, v in RFKILL_TYPES.items()}
_RFKILL_OP_CHANGE, _RFKILL_OP_CHANGE_ALL = 2, 3


def rfkill_list():
    out = []
    if not RFKILL_ROOT.exists():
        return out
    for d in sorted(RFKILL_ROOT.iterdir(), key=lambda p: int(p.name[6:] or 0)):
        out.append({
            "idx": int(d.name[6:]),
            "type": read(d / "type"),
            "name": read(d / "name"),
            "soft": read(d / "soft") == "1",
            "hard": read(d / "hard") == "1",
        })
    return out


def rfkill_set(blocked, idx=None, type_name=None):
    """Soft-block/unblock one device (idx) or a whole type ('all', 'wlan', ...) via /dev/rfkill."""
    if idx is not None:
        ev = struct.pack("IBBBB", idx, 0, _RFKILL_OP_CHANGE, int(blocked), 0)
    else:
        t = _RFKILL_TYPE_IDS[type_name or "all"]
        ev = struct.pack("IBBBB", 0, t, _RFKILL_OP_CHANGE_ALL, int(blocked), 0)
    fd = os.open("/dev/rfkill", os.O_WRONLY)
    try:
        os.write(fd, ev)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- audio

def audio_muted():
    rc, out = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    if rc != 0:
        return None
    return out.lower().endswith("yes")


def set_audio_muted(muted):
    rc, _ = run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])
    return rc == 0


# --------------------------------------------------------------------------- colour tint

# Per-channel linear multipliers (r, g, b) for the gamma ramps. On X11 the screen is first
# converted to grayscale by picom (see below), so the ramp turns brightness into the tint
# colour and nothing on screen disappears (e.g. blue text under a red tint). Without picom
# the ramps alone still tint, but pure colours the tint lacks go dark. Ramps are linear:
# xrandr's --gamma is an exponent, which left white at full white and caused colour fringing.
TINTS = {
    "red": (1.0, 0.0, 0.0),     # night-vision red, preserves dark adaptation
    "green": (0.0, 1.0, 0.0),   # NVG-friendly green
    "amber": (1.0, 0.45, 0.0),
    "gray": (1.0, 1.0, 1.0),    # grayscale only, plus dimming
}
# colour temperature fallback for gammastep (cannot produce green or gray)
_TINT_KELVIN = {"red": 1000, "green": 1000, "amber": 2300, "gray": 6500}


def _xrandr_set_ramps(mult):
    """Load a linear gamma ramp scaled by mult=(r, g, b) on every CRTC via libXrandr.

    Returns True on success. Ramps persist after the connection closes, like xrandr's.
    """
    import ctypes
    import ctypes.util

    lx, lr = ctypes.util.find_library("X11"), ctypes.util.find_library("Xrandr")
    if not (lx and lr):
        return False
    try:
        x11, xrr = ctypes.CDLL(lx), ctypes.CDLL(lr)
    except OSError:
        return False

    ulong, ushort_p = ctypes.c_ulong, ctypes.POINTER(ctypes.c_ushort)

    class ScreenResources(ctypes.Structure):
        _fields_ = [("timestamp", ulong), ("configTimestamp", ulong),
                    ("ncrtc", ctypes.c_int), ("crtcs", ctypes.POINTER(ulong)),
                    ("noutput", ctypes.c_int), ("outputs", ctypes.POINTER(ulong)),
                    ("nmode", ctypes.c_int), ("modes", ctypes.c_void_p)]

    class CrtcGamma(ctypes.Structure):
        _fields_ = [("size", ctypes.c_int),
                    ("red", ushort_p), ("green", ushort_p), ("blue", ushort_p)]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XDefaultRootWindow.restype = ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xrr.XRRGetScreenResourcesCurrent.restype = ctypes.POINTER(ScreenResources)
    xrr.XRRGetScreenResourcesCurrent.argtypes = [ctypes.c_void_p, ulong]
    xrr.XRRFreeScreenResources.argtypes = [ctypes.POINTER(ScreenResources)]
    xrr.XRRGetCrtcGammaSize.restype = ctypes.c_int
    xrr.XRRGetCrtcGammaSize.argtypes = [ctypes.c_void_p, ulong]
    xrr.XRRAllocGamma.restype = ctypes.POINTER(CrtcGamma)
    xrr.XRRAllocGamma.argtypes = [ctypes.c_int]
    xrr.XRRSetCrtcGamma.argtypes = [ctypes.c_void_p, ulong, ctypes.POINTER(CrtcGamma)]
    xrr.XRRFreeGamma.argtypes = [ctypes.POINTER(CrtcGamma)]

    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return False
    done = 0
    try:
        res = xrr.XRRGetScreenResourcesCurrent(dpy, x11.XDefaultRootWindow(dpy))
        if not res:
            return False
        try:
            for i in range(res.contents.ncrtc):
                crtc = res.contents.crtcs[i]
                size = xrr.XRRGetCrtcGammaSize(dpy, crtc)
                if size < 2:
                    continue
                g = xrr.XRRAllocGamma(size)
                if not g:
                    continue
                for ch, m in zip((g.contents.red, g.contents.green, g.contents.blue), mult):
                    m = min(max(float(m), 0.0), 1.0)
                    for j in range(size):
                        ch[j] = int(round(j * 65535 * m / (size - 1)))
                xrr.XRRSetCrtcGamma(dpy, crtc, g)
                xrr.XRRFreeGamma(g)
                done += 1
        finally:
            xrr.XRRFreeScreenResources(res)
        x11.XSync(dpy, 0)
    finally:
        x11.XCloseDisplay(dpy)
    return done > 0


# Gamma ramps are per channel, so they cannot mix colours into gray. Grayscale is instead
# drawn by picom with a GLSL shader while any tint is on. xfwm4's own compositor is switched
# off while picom runs and switched back on afterwards.
_GRAY_SHADER = """#version 330
in vec2 texcoord;
uniform sampler2D tex;
uniform float opacity;
vec4 default_post_processing(vec4 c);
vec4 window_shader() {
    vec4 c = texture2D(tex, texcoord / textureSize(tex, 0), 0);
    float y = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
    return default_post_processing(vec4(vec3(y), c.a) * opacity);
}
"""


def _runtime_dir():
    d = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/tbhub-{os.getuid()}") / "tbhub"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _gray_state_file():
    return _runtime_dir() / "grayscale.json"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    try:
        return "picom" in Path(f"/proc/{pid}/cmdline").read_text()
    except OSError:
        return True


def _xfwm_compositing(value=None):
    """Get (value=None) or set xfwm4's compositor. Returns the bool state or None if unknown."""
    if not shutil.which("xfconf-query"):
        return None
    if value is None:
        rc, out = run(["xfconf-query", "-c", "xfwm4", "-p", "/general/use_compositing"])
        return out == "true" if rc == 0 else None
    run(["xfconf-query", "-c", "xfwm4", "-p", "/general/use_compositing",
         "-s", "true" if value else "false"])
    return value


def grayscale_active():
    import json
    try:
        st = json.loads(_gray_state_file().read_text())
    except (OSError, ValueError):
        return False
    return _pid_alive(st.get("pid"))


def _grayscale_on():
    import json
    import time
    if grayscale_active():
        return True
    if not (os.environ.get("DISPLAY") and shutil.which("picom")):
        return False
    shader = _runtime_dir() / "grayscale.glsl"
    shader.write_text(_GRAY_SHADER)
    xfwm_was = _xfwm_compositing()
    if xfwm_was:
        _xfwm_compositing(False)
        time.sleep(0.5)  # let xfwm4 release the compositing selection
    try:
        proc = subprocess.Popen(
            ["picom", "--config", "/dev/null", "--backend", "glx",
             "--window-shader-fg", str(shader), "--no-fading-openclose"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        proc = None
    time.sleep(0.7)
    if proc is None or proc.poll() is not None:
        if xfwm_was:
            _xfwm_compositing(True)
        return False
    _gray_state_file().write_text(json.dumps({"pid": proc.pid, "xfwm_compositing": xfwm_was}))
    return True


def _grayscale_off():
    import json
    import signal
    import time
    f = _gray_state_file()
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        return
    pid = st.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
    if st.get("xfwm_compositing"):
        _xfwm_compositing(True)
    try:
        f.unlink()
    except OSError:
        pass


def set_tint(color=None, brightness=1.0):
    """Tint the screen ('red'|'green'|'amber'|'gray') with extra linear dimming, or reset if None.

    X11: picom grayscale shader + linear per-channel RandR gamma ramps.
    Elsewhere (wlroots): gammastep colour temperature.
    """
    if color is None:
        _grayscale_off()
    elif os.environ.get("DISPLAY") and not _grayscale_on():
        import sys
        print("tbhub: picom not found or failed to start (sudo apt install picom); "
              "using a plain tint", file=sys.stderr)
    if os.environ.get("DISPLAY"):
        if color is None:
            mult = (1.0, 1.0, 1.0)
        else:
            b = min(max(float(brightness), 0.0), 1.0)
            mult = tuple(c * b for c in TINTS.get(color, TINTS["red"]))
        if _xrandr_set_ramps(mult):
            return True
    if shutil.which("gammastep"):
        if color is None:
            return run(["gammastep", "-x"])[0] == 0
        return run(["gammastep", "-P", "-O", str(_TINT_KELVIN.get(color, 1000)),
                    "-b", f"{brightness:.2f}"])[0] == 0
    return False


# --------------------------------------------------------------------------- system info

def system_info():
    return {
        "vendor": read(DMI_ROOT / "sys_vendor"),
        "product": read(DMI_ROOT / "product_name"),
        "version": read(DMI_ROOT / "product_version"),
        "board": read(DMI_ROOT / "board_name"),
        "bios": read(DMI_ROOT / "bios_version"),
        "bios_date": read(DMI_ROOT / "bios_date"),
        "kernel": os.uname().release,
        "hostname": os.uname().nodename,
        "panasonic_driver": PANASONIC_ACPI.exists(),
    }
