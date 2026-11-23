"""Hotkey daemon: maps Toughbook Fn / A-button events (evdev) to tbhub actions.

The kernel's panasonic_laptop driver reports hotkeys it has no keymap entry for as
KEY_UNKNOWN preceded by an MSC_SCAN carrying the raw firmware code - so any Fn combo
can be bound by scan code even though X/desktop never sees a usable keysym.
"""

import json
import os
import re
import select
import shlex
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import modes

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY, EV_MSC, MSC_SCAN, KEY_UNKNOWN = 0x01, 0x04, 0x04, 240

KEYS_FILE = modes.CONFIG_DIR / "keys.json"

# Devices that carry Toughbook hotkeys. Regular keyboards are deliberately excluded.
DEFAULT_DEVICES = ["Panasonic Laptop Support", "Intel HID events", "Intel HID 5 button array"]

DEFAULT_KEYS = {
    "devices": DEFAULT_DEVICES,
    # Fill with `tbhub keys --learn`. Example:
    # {"device": "Panasonic Laptop Support", "scan": "0x1b", "action": "night toggle"}
    "bindings": [],
}


def load_keys():
    cfg = dict(DEFAULT_KEYS)
    try:
        cfg.update(json.loads(KEYS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_keys(cfg):
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYS_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def input_devices():
    """[(name, '/dev/input/eventN')] from /proc/bus/input/devices."""
    out, name = [], None
    try:
        text = Path("/proc/bus/input/devices").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        if line.startswith("N: Name="):
            name = line[8:].strip().strip('"')
        elif line.startswith("H: Handlers=") and name is not None:
            m = re.search(r"\bevent(\d+)\b", line)
            if m:
                out.append((name, f"/dev/input/event{m.group(1)}"))
            name = None
    return out


def _key_name(code):
    return _KEY_NAMES.get(code, f"KEY_{code}")


_KEY_NAMES = {240: "KEY_UNKNOWN", 224: "KEY_BRIGHTNESSDOWN", 225: "KEY_BRIGHTNESSUP",
              227: "KEY_SWITCHVIDEOMODE", 113: "KEY_MUTE", 114: "KEY_VOLUMEDOWN",
              115: "KEY_VOLUMEUP", 142: "KEY_SLEEP", 236: "KEY_BATTERY",
              238: "KEY_WLAN", 247: "KEY_RFKILL", 148: "KEY_PROG1", 149: "KEY_PROG2",
              202: "KEY_PROG3", 203: "KEY_PROG4", 116: "KEY_POWER", 152: "KEY_SCREENLOCK",
              120: "KEY_SCALE", 125: "KEY_LEFTMETA", 171: "KEY_CONFIG"}


class Listener:
    def __init__(self, names=None):
        """names=None -> every readable device (learn mode)."""
        self.names = names
        self.fds = {}            # fd -> (name, path)
        self.last_scan = {}      # fd -> last MSC_SCAN value
        self.denied = []

    def open_devices(self):
        open_paths = {p for _, p in self.fds.values()}
        self.denied = []
        for name, path in input_devices():
            if path in open_paths or (self.names is not None and name not in self.names):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError:
                self.denied.append((name, path))
                continue
            except OSError:
                continue
            self.fds[fd] = (name, path)

    def events(self, rescan=5.0):
        """Yield (device_name, keycode, scancode_or_None) on key *press*."""
        next_scan = 0
        while True:
            if time.monotonic() >= next_scan:
                self.open_devices()
                next_scan = time.monotonic() + rescan
            if not self.fds:
                time.sleep(rescan)
                continue
            ready, _, _ = select.select(list(self.fds), [], [], rescan)
            for fd in ready:
                try:
                    data = os.read(fd, EVENT_SIZE * 64)
                except OSError:  # device unplugged
                    os.close(fd)
                    self.fds.pop(fd, None)
                    continue
                for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, i)
                    if etype == EV_MSC and code == MSC_SCAN:
                        self.last_scan[fd] = value
                    elif etype == EV_KEY:
                        scan = self.last_scan.pop(fd, None)
                        if value == 1:
                            yield self.fds[fd][0], code, scan


# --------------------------------------------------------------------------- actions

def run_action(action):
    """Execute an action string such as 'night toggle', 'brightness +10', 'exec <cmd>'."""
    action = action.strip()
    if action.startswith("exec "):
        subprocess.Popen(action[5:], shell=True, start_new_session=True)
        return
    # run as a child process so a slow/blocking action (e.g. 'gui') never stalls the daemon
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=pkg_parent)
    subprocess.Popen([sys.executable, "-m", "tbhub", *shlex.split(action)],
                     env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _matches(b, name, code, scan):
    if b.get("device") and b["device"] != name:
        return False
    if "scan" in b:
        want = b["scan"]
        want = int(want, 0) if isinstance(want, str) else int(want)
        return scan is not None and scan == want
    if "key" in b:
        want = b["key"]
        return code == (int(want, 0) if isinstance(want, str) else int(want))
    return False


def _permission_hint(denied):
    names = ", ".join(sorted({n for n, _ in denied}))
    return (f"No permission to read: {names}.\n"
            "Run ./install.sh (adds a udev rule granting your login session access to the\n"
            "Toughbook hotkey devices only), then log out/in or replug.")


def learn(all_devices=False, seconds=None):
    cfg = load_keys()
    lst = Listener(None if all_devices else cfg["devices"])
    lst.open_devices()
    if lst.denied and not lst.fds:
        print(_permission_hint(lst.denied), file=sys.stderr)
        return 1
    print("Listening on:", ", ".join(sorted({n for n, _ in lst.fds.values()})))
    if lst.denied:
        print("(no access to:", ", ".join(sorted({n for n, _ in lst.denied})) + ")")
    print("Press the Fn / A-button combos now. Ctrl+C to stop.\n")
    end = time.monotonic() + seconds if seconds else None
    try:
        for name, code, scan in lst.events(rescan=1.0):
            scan_s = f"0x{scan:x}" if scan is not None else "-"
            print(f"{name:28s} key={code:<4d} {_key_name(code):22s} scan={scan_s}", flush=True)
            sel = {"device": name, "scan": scan_s} if code == KEY_UNKNOWN and scan is not None \
                else {"device": name, "key": code}
            print(f"    binding: {json.dumps({**sel, 'action': 'night toggle'})}", flush=True)
            if end and time.monotonic() > end:
                break
    except KeyboardInterrupt:
        pass
    return 0


def daemon():
    cfg = load_keys()
    if not cfg["bindings"]:
        print(f"No bindings in {KEYS_FILE}. Use `tbhub keys --learn` then "
              "`tbhub keys --bind ...`.", file=sys.stderr)
    lst = Listener(cfg["devices"])
    lst.open_devices()
    if lst.denied and not lst.fds:
        print(_permission_hint(lst.denied), file=sys.stderr)
    mtime = KEYS_FILE.stat().st_mtime if KEYS_FILE.exists() else 0
    for name, code, scan in lst.events():
        # hot-reload bindings when keys.json changes
        m = KEYS_FILE.stat().st_mtime if KEYS_FILE.exists() else 0
        if m != mtime:
            cfg, mtime = load_keys(), m
        for b in cfg["bindings"]:
            if _matches(b, name, code, scan):
                try:
                    run_action(b["action"])
                except Exception as e:  # never let one bad action kill the daemon
                    print(f"action {b['action']!r} failed: {e}", file=sys.stderr)
                break


def bind(action, device=None, scan=None, key=None):
    cfg = load_keys()
    b = {"device": device} if device else {}
    if scan is not None:
        b["scan"] = scan
    elif key is not None:
        b["key"] = int(key)
    else:
        raise ValueError("need --scan or --key")
    sel = {k: v for k, v in b.items()}
    cfg["bindings"] = [x for x in cfg["bindings"]
                       if {k: x.get(k) for k in sel} != sel]
    b["action"] = action
    cfg["bindings"].append(b)
    save_keys(cfg)
    return b
