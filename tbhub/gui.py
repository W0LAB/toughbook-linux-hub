"""GTK3 front-end for tbhub."""

import os
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import gps, gps_serial, hw, keys, modes  # noqa: E402

CSS = b"""
.mode-btn { padding: 6px 14px; font-weight: bold; }
.mode-btn:checked { background: #8b1a1a; color: #ffd0d0; }
.big-value { font-size: 20px; font-weight: bold; font-family: monospace; }
.mono { font-family: monospace; }
.dim { opacity: 0.65; }
.warn { color: #c77700; }
.section { font-weight: bold; margin-top: 12px; }
"""

ACTIONS = [
    ("night toggle", "Toggle night mode"),
    ("concealed toggle", "Toggle concealed mode"),
    ("brightness +10", "Brightness up"),
    ("brightness -10", "Brightness down"),
    ("radio airplane toggle", "Toggle airplane mode"),
    ("radio wifi toggle", "Toggle Wi-Fi"),
    ("radio bt toggle", "Toggle Bluetooth"),
    ("gui", "Open Toughbook Hub"),
]


def label(text="", css=None, xalign=0.0, selectable=False, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign, selectable=selectable)
    lb.set_line_wrap(wrap)
    if css:
        for c in css.split():
            lb.get_style_context().add_class(c)
    return lb


def page():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(18)
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.add(box)
    return sw, box


def kv_grid(rows):
    """rows: [(key, widget)] -> Gtk.Grid"""
    g = Gtk.Grid(column_spacing=18, row_spacing=6)
    for i, (k, w) in enumerate(rows):
        g.attach(label(k, "dim"), 0, i, 1, 1)
        g.attach(w, 1, i, 1, 1)
    return g


class Hub(Gtk.ApplicationWindow):
    def __init__(self, app, start_page=None):
        super().__init__(application=app, title="Toughbook Hub")
        self.set_default_size(940, 640)
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tbhub.svg")
        if os.path.exists(icon):
            self.set_icon_from_file(icon)
        else:
            self.set_icon_name("tbhub")
        self.bl = hw.Backlight()
        self.pana = hw.Panasonic()
        self.cfg = modes.load_config()
        self._syncing = False
        self._gps_state = None
        self._gps_dirty = False

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        hb = Gtk.HeaderBar(show_close_button=True, title="Toughbook Hub")
        info = hw.system_info()
        hb.set_subtitle(f"{info['product'] or ''}  ·  BIOS {info['bios'] or '?'}")
        self.btn_night = Gtk.ToggleButton(label="☾ Night")
        self.btn_conc = Gtk.ToggleButton(label="◼ Concealed")
        for b, m in ((self.btn_night, "night"), (self.btn_conc, "concealed")):
            b.get_style_context().add_class("mode-btn")
            b.connect("toggled", self.on_mode_toggled, m)
            hb.pack_end(b)
        self.set_titlebar(hb)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        side = Gtk.StackSidebar(stack=stack)
        side.set_size_request(150, -1)
        body = Gtk.Box()
        body.pack_start(side, False, False, 0)
        body.pack_start(stack, True, True, 0)
        self.add(body)

        for name, title, builder in (
            ("display", "Display", self.build_display),
            ("gps", "GPS", self.build_gps),
            ("power", "Power", self.build_power),
            ("wireless", "Wireless", self.build_wireless),
            ("hotkeys", "Hotkeys", self.build_hotkeys),
            ("device", "Device", self.build_device),
        ):
            stack.add_titled(builder(), name, title)
        self.stack = stack
        self._start_page = start_page

        self.gps_client = None
        self.start_gps()

        self.refresh()
        GLib.timeout_add(1000, self.tick)
        self.connect("destroy", lambda *_: self.gps_client and self.gps_client.stop())

    # ------------------------------------------------------------------ display
    def build_display(self):
        sw, box = page()
        box.pack_start(label("Backlight", "section"), False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale.connect("value-changed", self.on_brightness)
        self.scale.set_sensitive(self.bl.writable)
        box.pack_start(self.scale, False, False, 0)
        if not self.bl.writable:
            box.pack_start(label("No write access to the backlight - add yourself to the "
                                 "'video' group.", "warn", wrap=True), False, False, 0)

        box.pack_start(label("Night mode", "section"), False, False, 0)
        self.night_color = Gtk.ComboBoxText()
        for c in hw.TINTS:
            self.night_color.append(c, c.capitalize())
        self.night_color.set_active_id(self.cfg["night_color"])
        self.night_color.connect("changed", self.on_cfg, "night_color")
        self.night_level = Gtk.SpinButton.new_with_range(0.1, 50, 0.5)
        self.night_level.set_digits(1)
        self.night_level.set_value(self.cfg["night_backlight_pct"])
        self.night_level.connect("value-changed", self.on_cfg, "night_backlight_pct")
        box.pack_start(kv_grid([("Tint colour", self.night_color),
                                ("Backlight %", self.night_level)]), False, False, 0)

        box.pack_start(label("Concealed mode", "section"), False, False, 0)
        self.conc_mute = Gtk.Switch(active=self.cfg["concealed_mute"], halign=Gtk.Align.START)
        self.conc_mute.connect("notify::active", self.on_cfg, "concealed_mute")
        self.conc_radio = Gtk.Switch(active=self.cfg["concealed_radios_off"],
                                     halign=Gtk.Align.START)
        self.conc_radio.connect("notify::active", self.on_cfg, "concealed_radios_off")
        box.pack_start(kv_grid([("Mute audio", self.conc_mute),
                                ("Radio silence (Wi-Fi/BT/WWAN off)", self.conc_radio)]),
                       False, False, 0)

        box.pack_start(label("Firmware (panasonic_laptop)", "section"), False, False, 0)
        self.pana_widgets = {}
        rows, any_ro = [], False
        for name, a in self.pana.attrs().items():
            if name in ("sticky_key", "eco_mode", "mute", "cdpower"):
                w = Gtk.Switch(active=a["value"] == "1", halign=Gtk.Align.START)
                w.connect("notify::active", self.on_pana_switch, name)
            elif name in ("ac_brightness", "dc_brightness"):
                w = Gtk.SpinButton.new_with_range(0, 255, 1)
                w.set_value(int(a["value"]))
                w.connect("value-changed", self.on_pana_spin, name)
            else:
                w = label(a["value"], "mono")
            w.set_tooltip_text(a["desc"])
            if not a["writable"] and not isinstance(w, Gtk.Label):
                w.set_sensitive(False)
                any_ro = True
            self.pana_widgets[name] = w
            rows.append((a["label"], w))
        if rows:
            box.pack_start(kv_grid(rows), False, False, 0)
            if any_ro:
                box.pack_start(label("Firmware settings are root-only. Run ./install.sh to "
                                     "grant the 'video' group write access.", "dim",
                                     wrap=True), False, False, 0)
        else:
            box.pack_start(label("panasonic_laptop driver not loaded.", "dim"), False, False, 0)
        return sw

    def on_brightness(self, scale):
        if self._syncing:
            return
        try:
            self.bl.set_percent(scale.get_value())
        except OSError as e:
            self.toast(str(e))

    def on_cfg(self, widget, *args):
        key = args[-1]
        if isinstance(widget, Gtk.Switch):
            val = widget.get_active()
        elif isinstance(widget, Gtk.SpinButton):
            val = widget.get_value()
        else:
            val = widget.get_active_id()
        self.cfg[key] = val
        modes.save_config(self.cfg)
        # re-apply live if the relevant mode is on
        if key.startswith("night") and modes.is_active("night"):
            if key == "night_backlight_pct" and not modes.is_active("concealed"):
                self.bl.set_percent(val)
            if not modes.is_active("concealed"):
                hw.set_tint(self.cfg["night_color"], self.cfg["night_gamma"])

    def on_pana_switch(self, sw, _pspec, name):
        try:
            self.pana.set(name, "1" if sw.get_active() else "0")
        except OSError as e:
            self.toast(f"{name}: {e}")

    def on_pana_spin(self, spin, name):
        try:
            self.pana.set(name, int(spin.get_value()))
        except OSError as e:
            self.toast(f"{name}: {e}")

    def on_mode_toggled(self, btn, mode):
        if self._syncing:
            return
        try:
            modes.set_mode(mode, btn.get_active())
        except OSError as e:
            self.toast(str(e))
        self.refresh()

    # ------------------------------------------------------------------ GPS
    def build_gps(self):
        sw, box = page()
        top = Gtk.Box(spacing=28)
        top.pack_start(self.build_gps_source(), False, False, 0)
        top.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.gps_warn = label("", "warn", wrap=True)
        self.gps_warn.set_max_width_chars(48)
        self.gps_status = label("Connecting…", "big-value")
        right.pack_start(self.gps_status, False, False, 0)
        right.pack_start(self.gps_warn, False, False, 0)
        self.gps_vals = {}
        rows = []
        for k in ("Lat / Lon", "DMS", "MGRS", "UTM", "Altitude", "Speed / Track",
                  "Accuracy", "Satellites", "Receiver", "Source"):
            w = label("—", "mono", selectable=True)
            self.gps_vals[k] = w
            rows.append((k, w))
        right.pack_start(kv_grid(rows), False, False, 0)
        copy = Gtk.Button(label="Copy position", halign=Gtk.Align.START)
        copy.connect("clicked", self.on_copy_pos)
        right.pack_start(copy, False, False, 4)
        top.pack_start(right, True, True, 0)
        box.pack_start(top, False, False, 0)

        box.pack_start(label("Satellites", "section"), False, False, 0)
        self.sat_store = Gtk.ListStore(str, int, str, int, int, int, str)
        tv = Gtk.TreeView(model=self.sat_store)
        for i, t in enumerate(("System", "ID", "Used")):
            col = Gtk.TreeViewColumn(t, Gtk.CellRendererText(), text=[0, 1, 2][i])
            tv.append_column(col)
        tv.append_column(Gtk.TreeViewColumn("Elev°", Gtk.CellRendererText(), text=3))
        tv.append_column(Gtk.TreeViewColumn("Az°", Gtk.CellRendererText(), text=4))
        prog = Gtk.CellRendererProgress()
        c = Gtk.TreeViewColumn("Signal (dB-Hz)", prog, value=5, text=6)
        c.set_expand(True)
        tv.append_column(c)
        box.pack_start(tv, True, True, 0)
        return sw

    def build_gps_source(self):
        """Left-hand panel: source mode, gpsd address, serial device + baud."""
        c = gps.source_config(self.cfg)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.set_size_request(210, -1)

        def field(title, widget):
            col.pack_start(label(title, "dim"), False, False, 0)
            col.pack_start(widget, False, False, 0)
            widget.set_margin_bottom(8)

        col.pack_start(label("Source", "section"), False, False, 0)
        self.src_combo = Gtk.ComboBoxText()
        for k, t in (("auto", "Auto"), ("gpsd", "gpsd"), ("serial", "Serial port")):
            self.src_combo.append(k, t)
        self.src_combo.set_active_id(c["gps_source"])
        self.src_combo.connect("changed", lambda *_: self._src_sensitivity())
        self.src_hint = label("", "dim", wrap=True)
        self.src_hint.set_max_width_chars(28)
        col.pack_start(self.src_combo, False, False, 0)
        col.pack_start(self.src_hint, False, False, 0)
        self.src_hint.set_margin_bottom(8)

        self.src_host = Gtk.Entry(text=c["gpsd_host"], width_chars=12)
        self.src_port = Gtk.Entry(text=str(c["gpsd_port"]), width_chars=5, max_length=5)
        self.src_port.set_input_purpose(Gtk.InputPurpose.DIGITS)
        hp = Gtk.Box(spacing=4)
        hp.pack_start(self.src_host, True, True, 0)
        hp.pack_start(label(":"), False, False, 0)
        hp.pack_start(self.src_port, False, False, 0)
        self.src_gpsd_box = hp
        field("gpsd host : port", hp)

        self.src_dev = Gtk.Entry(text=c["gps_device"])
        devs = Gtk.ListStore(str)
        for d in gps_serial.list_serial_devices():
            devs.append([d])
        comp = Gtk.EntryCompletion(model=devs, minimum_key_length=0, inline_completion=True)
        comp.set_text_column(0)
        self.src_dev.set_completion(comp)
        self.src_dev.set_tooltip_text("Detected: " + ", ".join(r[0] for r in devs))
        field("Serial device", self.src_dev)

        self.src_baud = Gtk.ComboBoxText()
        for b in gps_serial.BAUDS:
            self.src_baud.append(str(b), str(b))
        self.src_baud.set_active_id(str(c["gps_baud"]))
        field("Baud rate", self.src_baud)

        apply = Gtk.Button(label="Apply")
        apply.connect("clicked", self.on_gps_source_apply)
        col.pack_start(apply, False, False, 0)
        self._src_sensitivity()
        return col

    _SRC_HINTS = {"auto": "gpsd when it has a receiver, otherwise the serial port.",
                  "gpsd": "Read from a gpsd server, local or remote.",
                  "serial": "Read the receiver directly. gpsd must not be using it."}

    def _src_sensitivity(self):
        s = self.src_combo.get_active_id()
        self.src_hint.set_text(self._SRC_HINTS.get(s, ""))
        self.src_gpsd_box.set_sensitive(s in ("auto", "gpsd"))
        self.src_dev.set_sensitive(s in ("auto", "serial"))
        self.src_baud.set_sensitive(s in ("auto", "serial"))

    def on_gps_source_apply(self, _btn):
        if not self.src_port.get_text().strip().isdigit():
            self.toast("gpsd port must be a number")
            return
        baud = self.src_baud.get_active_id()
        self.cfg.update(gps_source=self.src_combo.get_active_id(),
                        gpsd_host=self.src_host.get_text().strip() or "127.0.0.1",
                        gpsd_port=int(self.src_port.get_text().strip() or 2947),
                        gps_device=self.src_dev.get_text().strip() or "/dev/gps0",
                        gps_baud=int(baud) if baud else 9600)
        modes.save_config(self.cfg)
        self.start_gps()
        self.toast(f"GPS source: {self.cfg['gps_source']}")

    def start_gps(self):
        if self.gps_client:
            self.gps_client.stop()
        self._gps_state = None
        if hasattr(self, "gps_vals"):
            for w in self.gps_vals.values():
                w.set_text("—")
            self.sat_store.clear()
            self.gps_status.set_text("Connecting…")
            self.gps_warn.set_text("")
        self.gps_client = gps.GpsSource(self.cfg, on_update=self._on_gps)
        self.gps_client.start()

    def _on_gps(self, st):  # called from GPS thread
        if self.gps_client is None or st is not self.gps_client.state:
            return  # update from a stopped (previous) source
        self._gps_state = st
        self._gps_dirty = True

    def update_gps(self):
        st = self._gps_state
        if st is None or not self._gps_dirty:
            return
        self._gps_dirty = False
        v = self.gps_vals
        v["Source"].set_text(st.source or "—")
        if not st.connected:
            self.gps_status.set_text("No GPS source")
            self.gps_warn.set_text(st.error or "")
            return
        used, seen = st.sat_counts()
        self.gps_status.set_text(gps.FIX_MODES.get(st.mode, "?"))
        self.gps_warn.set_text(st.error or st.time_warning() or "")
        v["Satellites"].set_text(f"{used} used / {seen} visible")
        if st.device:
            v["Receiver"].set_text(f"{st.device.get('driver', '?')}  {st.device.get('path', '')}")
        t = st.tpv
        if not st.has_fix:
            for k in ("Lat / Lon", "DMS", "MGRS", "UTM", "Altitude",
                      "Speed / Track", "Accuracy"):
                v[k].set_text("—")
        else:
            lat, lon = t["lat"], t["lon"]
            v["Lat / Lon"].set_text(f"{lat:.6f}, {lon:.6f}")
            v["DMS"].set_text(f"{gps.fmt_dms(lat, True)}  {gps.fmt_dms(lon, False)}")
            v["MGRS"].set_text(gps.latlon_to_mgrs(lat, lon) or "—")
            v["UTM"].set_text(gps.fmt_utm(lat, lon))
            v["Altitude"].set_text(f"{t['altMSL']:.1f} m MSL" if "altMSL" in t else "—")
            v["Speed / Track"].set_text(
                f"{t['speed'] * 3.6:.1f} km/h  {t.get('track', 0):.0f}°" if "speed" in t else "—")
            v["Accuracy"].set_text(gps.fmt_accuracy(st))
        self.sat_store.clear()
        for s in sorted(st.satellites(), key=lambda s: (not s.get("used"), -s.get("ss", 0))):
            ss = s.get("ss") or 0
            self.sat_store.append([gps.GNSS_NAMES.get(s.get("gnssid"), "?"),
                                   s.get("svid", s.get("PRN", 0)),
                                   "✓" if s.get("used") else "",
                                   int(s.get("el") or 0), int(s.get("az") or 0),
                                   min(100, int(ss * 2)), f"{ss:.0f}"])

    def on_copy_pos(self, _btn):
        st = self._gps_state
        if not st or not st.has_fix:
            return
        lat, lon = st.tpv["lat"], st.tpv["lon"]
        text = f"{lat:.6f}, {lon:.6f}  ({gps.latlon_to_mgrs(lat, lon)})"
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.toast("Position copied")

    # ------------------------------------------------------------------ power
    def build_power(self):
        sw, box = page()
        self.pw_level = Gtk.LevelBar(min_value=0, max_value=100)
        self.pw_level.set_size_request(-1, 18)
        self.pw_big = label("", "big-value")
        box.pack_start(self.pw_big, False, False, 0)
        box.pack_start(self.pw_level, False, False, 0)
        self.pw_vals = {}
        rows = []
        for k in ("Power source", "Status", "Draw", "Time remaining", "Energy", "Health",
                  "Cycles", "Battery"):
            w = label("—", "mono")
            self.pw_vals[k] = w
            rows.append((k, w))
        box.pack_start(kv_grid(rows), False, False, 8)
        return sw

    def update_power(self):
        p = hw.power_status()
        v = self.pw_vals
        v["Power source"].set_text("AC adapter" if p["ac_online"] else "Battery")
        if not p["batteries"]:
            self.pw_big.set_text("No battery")
            return
        b = p["batteries"][0]
        self.pw_big.set_text(f"{b['capacity']}%")
        self.pw_level.set_value(b["capacity"] or 0)
        v["Status"].set_text(b["status"] or "—")
        v["Draw"].set_text(f"{b['power_w']:.1f} W" if b["power_w"] else "—")
        if b["hours"]:
            h = b["hours"]
            v["Time remaining"].set_text(f"{int(h)} h {int(h * 60) % 60:02d} min")
        else:
            v["Time remaining"].set_text("—")
        if b["energy_wh"] is not None:
            v["Energy"].set_text(f"{b['energy_wh']:.1f} / {b['full_wh']:.1f} Wh "
                                 f"(design {b['design_wh']:.1f})")
        v["Health"].set_text(f"{b['health']:.0f}%" if b["health"] else "—")
        v["Cycles"].set_text(str(b["cycles"]) if b["cycles"] else "—")
        v["Battery"].set_text(f"{b['manufacturer'] or ''} {b['model'] or ''} "
                              f"{b['technology'] or ''}".strip())

    # ------------------------------------------------------------------ wireless
    def build_wireless(self):
        sw, box = page()
        self.radio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.airplane = Gtk.Switch(halign=Gtk.Align.START)
        self.airplane.connect("notify::active", self.on_airplane)
        box.pack_start(kv_grid([("Airplane mode", self.airplane)]), False, False, 0)
        box.pack_start(label("Radios", "section"), False, False, 0)
        box.pack_start(self.radio_box, False, False, 0)
        self.radio_switches = {}
        return sw

    def update_wireless(self):
        devs = hw.rfkill_list()
        if set(self.radio_switches) != {d["idx"] for d in devs}:
            for c in self.radio_box.get_children():
                self.radio_box.remove(c)
            self.radio_switches = {}
            rows = []
            for d in devs:
                s = Gtk.Switch(halign=Gtk.Align.START)
                s.connect("notify::active", self.on_radio, d["idx"])
                self.radio_switches[d["idx"]] = s
                rows.append((f"{d['type']} ({d['name']})", s))
            self.radio_box.add(kv_grid(rows))
            self.radio_box.show_all()
        for d in devs:
            s = self.radio_switches[d["idx"]]
            s.set_active(not d["soft"] and not d["hard"])
            s.set_sensitive(not d["hard"])
            s.set_tooltip_text("Hardware switch is off" if d["hard"] else None)
        self.airplane.set_active(bool(devs) and all(d["soft"] for d in devs))

    def on_radio(self, sw, _pspec, idx):
        if self._syncing:
            return
        try:
            hw.rfkill_set(not sw.get_active(), idx=idx)
        except OSError as e:
            self.toast(f"rfkill: {e}")

    def on_airplane(self, sw, _pspec):
        if self._syncing:
            return
        try:
            hw.rfkill_set(sw.get_active(), type_name="all")
        except OSError as e:
            self.toast(f"rfkill: {e}")

    # ------------------------------------------------------------------ hotkeys
    def build_hotkeys(self):
        sw, box = page()
        box.pack_start(label(
            "Bind Toughbook Fn combinations and A-buttons to actions. Click “Learn”, press the "
            "key (e.g. Fn+A3), choose an action, then Bind. The tbhub hotkey service (started "
            "at login by install.sh) picks up changes immediately.", wrap=True), False, False, 0)
        row = Gtk.Box(spacing=8)
        self.learn_btn = Gtk.Button(label="Learn key…")
        self.learn_btn.connect("clicked", self.on_learn)
        self.learn_lbl = label("—", "mono")
        row.pack_start(self.learn_btn, False, False, 0)
        row.pack_start(self.learn_lbl, True, True, 0)
        box.pack_start(row, False, False, 4)
        row2 = Gtk.Box(spacing=8)
        self.action_combo = Gtk.ComboBoxText()
        for a, t in ACTIONS:
            self.action_combo.append(a, t)
        self.action_combo.set_active(0)
        self.bind_btn = Gtk.Button(label="Bind", sensitive=False)
        self.bind_btn.connect("clicked", self.on_bind)
        row2.pack_start(self.action_combo, False, False, 0)
        row2.pack_start(self.bind_btn, False, False, 0)
        box.pack_start(row2, False, False, 0)
        box.pack_start(label("Current bindings", "section"), False, False, 0)
        self.bind_store = Gtk.ListStore(str, str, str)
        tv = Gtk.TreeView(model=self.bind_store)
        for i, t in enumerate(("Device", "Code", "Action")):
            tv.append_column(Gtk.TreeViewColumn(t, Gtk.CellRendererText(), text=i))
        self.bind_view = tv
        box.pack_start(tv, False, False, 0)
        rm = Gtk.Button(label="Remove selected", halign=Gtk.Align.START)
        rm.connect("clicked", self.on_unbind)
        box.pack_start(rm, False, False, 0)
        self._learned = None
        self.load_bindings()
        return sw

    def load_bindings(self):
        self.bind_store.clear()
        for b in keys.load_keys()["bindings"]:
            code = f"scan {b['scan']}" if "scan" in b else f"key {b.get('key')}"
            self.bind_store.append([b.get("device", "any"), code, b["action"]])

    def on_learn(self, _btn):
        self.learn_btn.set_sensitive(False)
        self.learn_lbl.set_text("Press the key now…")
        lst = keys.Listener(keys.load_keys()["devices"])
        lst.open_devices()
        if not lst.fds:
            self.learn_lbl.set_text("No access to hotkey devices - run ./install.sh, then "
                                    "log out and back in.")
            self.learn_btn.set_sensitive(True)
            return

        def worker():
            for name, code, scan in lst.events(rescan=1.0):
                GLib.idle_add(self._learned_key, name, code, scan)
                break
            for fd in list(lst.fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _learned_key(self, name, code, scan):
        self._learned = (name, code, scan)
        desc = f"scan 0x{scan:x}" if code == keys.KEY_UNKNOWN and scan is not None \
            else f"{keys._key_name(code)} ({code})"
        self.learn_lbl.set_text(f"{name}: {desc}")
        self.learn_btn.set_sensitive(True)
        self.bind_btn.set_sensitive(True)

    def on_bind(self, _btn):
        if not self._learned:
            return
        name, code, scan = self._learned
        action = self.action_combo.get_active_id()
        if code == keys.KEY_UNKNOWN and scan is not None:
            keys.bind(action, device=name, scan=f"0x{scan:x}")
        else:
            keys.bind(action, device=name, key=code)
        self.load_bindings()
        self.toast(f"Bound to “{action}”")

    def on_unbind(self, _btn):
        model, it = self.bind_view.get_selection().get_selected()
        if it is None:
            return
        idx = model.get_path(it).get_indices()[0]
        cfg = keys.load_keys()
        del cfg["bindings"][idx]
        keys.save_keys(cfg)
        self.load_bindings()

    # ------------------------------------------------------------------ device
    def build_device(self):
        sw, box = page()
        info = hw.system_info()
        rows = [(k, label(str(v), "mono", selectable=True)) for k, v in (
            ("Vendor", info["vendor"]), ("Model", info["product"]),
            ("Version", info["version"]), ("Board", info["board"]),
            ("BIOS", f"{info['bios']} ({info['bios_date']})"),
            ("Kernel", info["kernel"]), ("Hostname", info["hostname"]),
            ("panasonic_laptop", "loaded" if info["panasonic_driver"] else "not loaded"),
            ("Backlight device", self.bl.name or "none"),
        )]
        box.pack_start(kv_grid(rows), False, False, 0)
        return sw

    # ------------------------------------------------------------------ misc
    def toast(self, msg):
        self.get_titlebar().set_subtitle(msg)
        GLib.timeout_add_seconds(4, self._reset_subtitle)

    def _reset_subtitle(self):
        info = hw.system_info()
        self.get_titlebar().set_subtitle(f"{info['product'] or ''}  ·  BIOS {info['bios'] or '?'}")
        return False

    def refresh(self):
        self._syncing = True
        try:
            active = modes.active_modes()
            self.btn_night.set_active("night" in active)
            self.btn_conc.set_active("concealed" in active)
            Gtk.Settings.get_default().set_property(
                "gtk-application-prefer-dark-theme", bool(active))
            if self.bl.available:
                self.scale.set_value(self.bl.percent)
            self.update_wireless()
        finally:
            self._syncing = False

    def tick(self):
        self.refresh()
        self.update_gps()
        self.update_power()
        return True


class App(Gtk.Application):
    def __init__(self, start_page=None):
        super().__init__(application_id="io.github.tbhub")
        self.start_page = start_page

    def do_activate(self):
        win = self.get_active_window() or Hub(self, self.start_page)
        win.show_all()
        if self.start_page:
            win.stack.set_visible_child_name(self.start_page)
        win.present()


def main(page=None):
    return App(page).run([])
