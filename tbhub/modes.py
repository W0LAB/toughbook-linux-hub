"""Night mode and Concealed mode, with save/restore of prior state across processes."""

import json
import os
from pathlib import Path

from . import hw
from .gps import DEFAULT_SOURCE as _GPS_DEFAULTS

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tbhub"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tbhub"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_CONFIG = {
    # Night mode: very dim backlight + red shift (preserves dark adaptation)
    "night_backlight_pct": 2.0,
    "night_color": "red",          # red | green | amber | gray
    "night_gamma": 0.85,            # extra software dimming on top of backlight
    # Concealed mode: lowest possible light and no sound
    "concealed_backlight_raw": 1,   # 0 blanks some panels completely; 1 = barely lit
    "concealed_color": "red",
    "concealed_gamma": 0.6,
    "concealed_mute": True,
    "concealed_radios_off": False,  # also soft-block Wi-Fi/BT/WWAN (radio silence)
    # GPS source: gps_source auto|gpsd|serial, gpsd_host/port, gps_device/baud
    **_GPS_DEFAULTS,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(STATE_FILE)


def is_active(mode):
    return mode in _load_state()


def active_modes():
    return [m for m in ("night", "concealed") if m in _load_state()]


def _apply_tint(cfg, st):
    """Tint for the strongest active mode, or reset."""
    if "concealed" in st:
        hw.set_tint(cfg["concealed_color"], cfg["concealed_gamma"])
    elif "night" in st:
        hw.set_tint(cfg["night_color"], cfg["night_gamma"])
    else:
        hw.set_tint(None)


# --------------------------------------------------------------------------- night

def night_on():
    st, cfg, bl = _load_state(), load_config(), hw.Backlight()
    if "night" in st:
        return
    if "concealed" in st:
        # backlight is at the concealed level; the real "before" value is what concealed saved
        before = st["concealed"].get("backlight_raw")
    else:
        before = bl.raw if bl.available else None
    st["night"] = {"backlight_raw": before}
    _save_state(st)
    if bl.available and "concealed" not in st:
        bl.set_percent(cfg["night_backlight_pct"])
    _apply_tint(cfg, st)


def night_off():
    st, cfg, bl = _load_state(), load_config(), hw.Backlight()
    saved = st.pop("night", None)
    if saved is None:
        return
    _save_state(st)
    if "concealed" in st:
        # concealed is still on: make it restore to the pre-night brightness later
        st["concealed"]["backlight_raw"] = saved.get("backlight_raw")
        _save_state(st)
    elif bl.available and saved.get("backlight_raw") is not None:
        bl.raw = saved["backlight_raw"]
    _apply_tint(cfg, st)


def cycle_night_color():
    """Switch night mode to the next tint colour, save it, and re-apply it if visible."""
    cfg = load_config()
    colors = list(hw.TINTS)
    cur = cfg.get("night_color")
    cfg["night_color"] = colors[(colors.index(cur) + 1) % len(colors)] if cur in colors else colors[0]
    save_config(cfg)
    st = _load_state()
    if "night" in st and "concealed" not in st:
        _apply_tint(cfg, st)
    return cfg["night_color"]


# --------------------------------------------------------------------------- concealed

def concealed_on():
    st, cfg, bl = _load_state(), load_config(), hw.Backlight()
    if "concealed" in st:
        return
    saved = {
        "backlight_raw": bl.raw if bl.available else None,
        "muted": hw.audio_muted(),
        "rfkill": {str(d["idx"]): d["soft"] for d in hw.rfkill_list()},
    }
    st["concealed"] = saved
    _save_state(st)
    if bl.available:
        bl.raw = cfg["concealed_backlight_raw"]
    if cfg["concealed_mute"]:
        hw.set_audio_muted(True)
    if cfg["concealed_radios_off"]:
        try:
            hw.rfkill_set(True, type_name="all")
        except OSError:
            pass
    _apply_tint(cfg, st)


def concealed_off():
    st, cfg, bl = _load_state(), load_config(), hw.Backlight()
    saved = st.pop("concealed", None)
    if saved is None:
        return
    _save_state(st)
    if bl.available and saved.get("backlight_raw") is not None:
        if "night" in st:
            bl.set_percent(cfg["night_backlight_pct"])
        else:
            bl.raw = saved["backlight_raw"]
    if saved.get("muted") is not None:
        hw.set_audio_muted(saved["muted"])
    if cfg["concealed_radios_off"]:
        for idx, soft in saved.get("rfkill", {}).items():
            try:
                hw.rfkill_set(soft, idx=int(idx))
            except OSError:
                pass
    _apply_tint(cfg, st)


# --------------------------------------------------------------------------- generic

_FUNCS = {"night": (night_on, night_off), "concealed": (concealed_on, concealed_off)}


def set_mode(mode, on):
    _FUNCS[mode][0 if on else 1]()


def toggle(mode):
    set_mode(mode, not is_active(mode))
    return is_active(mode)


def reset():
    """Emergency reset: drop saved state, clear tint, unmute. Leaves backlight at 50% if dim."""
    st = _load_state()
    for m in ("concealed", "night"):
        if m in st:
            set_mode(m, False)
    hw.set_tint(None)
    bl = hw.Backlight()
    if bl.available and bl.percent < 5:
        bl.set_percent(50)
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
