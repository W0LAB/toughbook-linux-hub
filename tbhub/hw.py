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

def _outputs():
    rc, out = run(["xrandr", "--listmonitors"])
    if rc != 0:
        return []
    return [line.split()[-1] for line in out.splitlines()[1:] if line.strip()]


# xrandr per-channel gamma (r:g:b). Lower = that channel is pushed toward black.
TINTS = {
    "red": "1:0.12:0.08",     # night-vision red, preserves dark adaptation
    "green": "0.1:1:0.1",     # NVG-friendly green
    "amber": "1:0.55:0.1",
}
# colour temperature fallback for gammastep (cannot produce green)
_TINT_KELVIN = {"red": 1000, "green": 1000, "amber": 2300}


def set_tint(color=None, brightness=1.0):
    """Tint the screen ('red'|'green'|'amber') with extra gamma dimming, or reset if color is None.

    X11: xrandr per-channel gamma. Elsewhere (wlroots): gammastep colour temperature.
    """
    if os.environ.get("DISPLAY") and shutil.which("xrandr"):
        outs = _outputs()
        if outs:
            ok = True
            for o in outs:
                if color is None:
                    args = ["--gamma", "1:1:1", "--brightness", "1"]
                else:
                    args = ["--gamma", TINTS.get(color, TINTS["red"]),
                            "--brightness", f"{brightness:.2f}"]
                ok &= run(["xrandr", "--output", o] + args)[0] == 0
            if ok:
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
