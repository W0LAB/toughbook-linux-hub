"""tbhub command line. With no arguments, launches the GUI."""

import argparse
import json
import os
import sys
import time

from . import gps, hw, modes

ON_OFF = ["on", "off", "toggle", "status"]


def _mode_cmd(mode, arg):
    if arg == "cycle":
        print(f"{mode} colour: {modes.cycle_night_color()}")
        return
    if arg == "toggle":
        on = modes.toggle(mode)
    elif arg in ("on", "off"):
        modes.set_mode(mode, arg == "on")
        on = arg == "on"
    else:
        on = modes.is_active(mode)
    print(f"{mode}: {'on' if on else 'off'}")


def cmd_brightness(a):
    bl = hw.Backlight()
    if not bl.available:
        sys.exit("no backlight device")
    if a.value:
        v = a.value.rstrip("%")
        try:
            if v[0] in "+-":
                bl.set_percent(bl.percent + float(v))
            else:
                bl.set_percent(float(v))
        except PermissionError:
            sys.exit(f"no write access to {bl.path}/brightness (need 'video' group)")
    print(f"{bl.name}: {bl.percent:.1f}% ({bl.raw}/{bl.max})")


def _gps_cfg(a):
    cfg = modes.load_config()
    for opt, key in (("source", "gps_source"), ("host", "gpsd_host"), ("port", "gpsd_port"),
                     ("device", "gps_device"), ("baud", "gps_baud")):
        if getattr(a, opt) is not None:
            cfg[key] = getattr(a, opt)
    return cfg


def cmd_gps(a):
    if a.devices:
        from . import gps_serial
        for d in gps_serial.list_serial_devices():
            real = os.path.realpath(d)
            print(d if real == d else f"{d} -> {real}")
        return
    cfg = _gps_cfg(a)
    if a.save:
        modes.save_config(cfg)
        c = gps.source_config(cfg)
        print(f"saved GPS source: {c['gps_source']} (gpsd {c['gpsd_host']}:{c['gpsd_port']}, "
              f"serial {c['gps_device']} @ {c['gps_baud']})")
    if a.watch:
        def show(st):
            line = _gps_line(st)
            print("\r\033[K" + line, end="", flush=True)
        c = gps.GpsSource(cfg, on_update=show)
        c.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()
        return
    st = gps.snapshot(cfg, timeout=a.timeout)
    if a.json:
        print(json.dumps(st.to_dict(), indent=2))
        return
    if not st.connected:
        sys.exit(f"{st.source or 'GPS'}: {st.error}")
    t = st.tpv
    print(f"Source:     {st.source}")
    if st.error:
        print(f"WARNING:    {st.error}")
    used, seen = st.sat_counts()
    print(f"Fix:        {gps.FIX_MODES.get(st.mode, '?')}  ({used} used / {seen} visible)")
    if st.has_fix:
        lat, lon = t["lat"], t["lon"]
        print(f"Lat/Lon:    {lat:.6f}, {lon:.6f}")
        print(f"DMS:        {gps.fmt_dms(lat, True)}  {gps.fmt_dms(lon, False)}")
        print(f"MGRS:       {gps.latlon_to_mgrs(lat, lon)}")
        print(f"UTM:        {gps.fmt_utm(lat, lon)}")
        if "altMSL" in t:
            print(f"Altitude:   {t['altMSL']:.1f} m MSL")
        print(f"Accuracy:   {gps.fmt_accuracy(st)}")
        if "speed" in t:
            print(f"Speed:      {t['speed'] * 3.6:.1f} km/h  track {t.get('track', 0):.0f}°")
    if st.device:
        print(f"Receiver:   {st.device.get('driver', '?')} on {st.device.get('path', '?')}")
    w = st.time_warning()
    if w:
        print(f"WARNING:    {w}")


def _gps_line(st):
    if not st.connected:
        return f"{st.source or 'GPS'}: {st.error or 'connecting...'}"
    used, seen = st.sat_counts()
    if not st.has_fix:
        return f"{gps.FIX_MODES.get(st.mode, '?')}  sats {used}/{seen}"
    t = st.tpv
    return (f"{gps.FIX_MODES[st.mode]}  {t['lat']:.6f},{t['lon']:.6f}  "
            f"{gps.latlon_to_mgrs(t['lat'], t['lon'])}  {gps.fmt_accuracy(st)}  "
            f"sats {used}/{seen}")


def cmd_radio(a):
    if a.target is None:
        for d in hw.rfkill_list():
            state = "hard-blocked" if d["hard"] else ("off" if d["soft"] else "on")
            print(f"{d['idx']:3d}  {d['type']:10s} {d['name']:20s} {state}")
        return
    airplane = a.target == "airplane"
    tname = {"wifi": "wlan", "bt": "bluetooth", "airplane": "all"}.get(a.target, a.target)
    sel = [d for d in hw.rfkill_list() if tname == "all" or d["type"] == tname]
    if a.state == "toggle":
        blocked = not (sel and all(d["soft"] for d in sel))
    else:  # "on" means radio on, except airplane where "on" means radios off
        blocked = (a.state == "on") if airplane else (a.state == "off")
    hw.rfkill_set(blocked, type_name=tname)
    if airplane:
        print(f"airplane: {'on' if blocked else 'off'}")
    else:
        print(f"{a.target}: {'off' if blocked else 'on'}")


def cmd_status(a):
    bl = hw.Backlight()
    pw = hw.power_status()
    data = {
        "system": hw.system_info(),
        "backlight": {"device": bl.name, "percent": round(bl.percent, 1), "raw": bl.raw,
                      "max": bl.max} if bl.available else None,
        "modes": modes.active_modes(),
        "power": pw,
        "panasonic": hw.Panasonic().attrs(),
        "radios": hw.rfkill_list(),
        "audio_muted": hw.audio_muted(),
    }
    if a.json:
        print(json.dumps(data, indent=2))
        return
    s = data["system"]
    print(f"{s['vendor']} {s['product']} (board {s['board']}, BIOS {s['bios']})")
    if bl.available:
        print(f"Backlight:  {bl.percent:.0f}%")
    print(f"Modes:      {', '.join(data['modes']) or 'normal'}")
    print(f"AC:         {'online' if pw['ac_online'] else 'battery'}")
    for b in pw["batteries"]:
        extra = f", {b['hours']:.1f} h left" if b["hours"] else ""
        hl = f", health {b['health']:.0f}%" if b["health"] else ""
        print(f"{b['name']}:       {b['capacity']}% {b['status']}{extra}{hl}")
    for name, v in data["panasonic"].items():
        print(f"{v['label'] + ':':28s}{v['value']}{'' if v['writable'] else '  (read-only)'}")


def cmd_pana(a):
    p = hw.Panasonic()
    if not p.available:
        sys.exit("panasonic_laptop driver not loaded")
    if a.value is None:
        print(p.get(a.attr))
    else:
        try:
            p.set(a.attr, a.value)
        except PermissionError:
            sys.exit(f"no write access to {a.attr}; run ./install.sh to install the udev rule")


def cmd_keys(a):
    from . import keys
    if a.learn:
        return keys.learn(all_devices=a.all)
    if a.bind:
        b = keys.bind(a.bind, device=a.device, scan=a.scan, key=a.key)
        print("bound:", json.dumps(b))
        return
    if a.list:
        print(json.dumps(keys.load_keys(), indent=2))
        return
    keys.daemon()


def build_parser():
    p = argparse.ArgumentParser(prog="tbhub", description="Toughbook hub for Linux")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("gui", help="open the graphical hub (default)")
    sp.add_argument("--page", choices=["display", "gps", "power", "wireless", "hotkeys", "device"])

    for m, h in (("night", "dim + red/green/amber/gray tint"), ("concealed", "minimum light, muted")):
        sp = sub.add_parser(m, help=h)
        sp.add_argument("state", nargs="?", default="toggle",
                        choices=ON_OFF + (["cycle"] if m == "night" else []),
                        help="cycle = next tint colour" if m == "night" else None)

    sp = sub.add_parser("reset", help="clear tint, leave all modes, restore usable brightness")

    sp = sub.add_parser("brightness", help="get/set backlight: 40, 40%%, +10, -10")
    sp.add_argument("value", nargs="?")

    sp = sub.add_parser("gps", help="show GPS position (MGRS/UTM/DMS)")
    sp.add_argument("-w", "--watch", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("-t", "--timeout", type=float, default=5.0)
    sp.add_argument("-s", "--source", choices=gps.SOURCES, help="auto (default), gpsd, serial")
    sp.add_argument("--host", help="gpsd host (default 127.0.0.1)")
    sp.add_argument("--port", type=int, help="gpsd port (default 2947)")
    sp.add_argument("--device", help="serial device (default /dev/gps0)")
    sp.add_argument("--baud", type=int, help="serial baud rate (default 9600)")
    sp.add_argument("--save", action="store_true", help="remember these source options")
    sp.add_argument("--devices", action="store_true", help="list serial GNSS candidates")

    sp = sub.add_parser("radio", help="list or switch radios: wifi|bt|wwan|all|airplane")
    sp.add_argument("target", nargs="?")
    sp.add_argument("state", nargs="?", default="toggle", choices=["on", "off", "toggle"])

    sp = sub.add_parser("status", help="system summary")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("pana", help="read/write a Panasonic ACPI attribute (e.g. sticky_key)")
    sp.add_argument("attr", choices=sorted(hw.PANASONIC_ATTRS))
    sp.add_argument("value", nargs="?")

    sp = sub.add_parser("keys", help="hotkey daemon; --learn to discover Fn/A-button codes")
    sp.add_argument("--learn", action="store_true", help="print codes of pressed hotkeys")
    sp.add_argument("--all", action="store_true", help="learn on every input device")
    sp.add_argument("--list", action="store_true", help="show bindings")
    sp.add_argument("--bind", metavar="ACTION", help="e.g. 'night toggle' or 'exec <cmd>'")
    sp.add_argument("--device", help="device name (from --learn)")
    sp.add_argument("--scan", help="scan code, e.g. 0x1b")
    sp.add_argument("--key", help="key code")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd in (None, "gui"):
        from . import gui
        return gui.main(getattr(a, "page", None))
    if a.cmd in ("night", "concealed"):
        return _mode_cmd(a.cmd, a.state)
    if a.cmd == "reset":
        modes.reset()
        print("reset")
        return
    return {"brightness": cmd_brightness, "gps": cmd_gps, "radio": cmd_radio,
            "status": cmd_status, "pana": cmd_pana, "keys": cmd_keys}[a.cmd](a)
