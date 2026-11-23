# toughbook-linux-hub (`tbhub`)

An unofficial Linux companion app for Panasonic Toughbooks, in the spirit of PC Hub on Windows:
night and concealed modes, GPS with MGRS, Fn/A-button hotkeys, battery health and radio switches.
Built and tested on an FZ-55 mk2 (Debian 13, XFCE/X11). It should work on other Toughbooks that use
the `panasonic_laptop` driver; reports from other models are welcome.

> Not affiliated with or endorsed by Panasonic. "Toughbook" and "Panasonic" are trademarks of
> Panasonic Corporation.

| Feature | How |
|---|---|
| **Night mode**: very dim backlight + red/green/amber screen tint | `intel_backlight` + xrandr per-channel gamma (gammastep fallback on Wayland) |
| **Concealed mode**: minimum light, audio muted, optional radio silence | backlight raw 1, `pactl`, `/dev/rfkill` |
| **GPS**: fix, lat/lon, DMS, **MGRS**, UTM, accuracy, per-satellite signal | gpsd (local or remote), or the serial port directly (u-blox UBX + NMEA) |
| **Hotkeys**: bind Fn / A-button combos (e.g. Fn+A3) to any action | evdev listener on the `Panasonic Laptop Support` device |
| Power: charge, draw, time left, battery health, cycles | `/sys/class/power_supply` |
| Wireless: per-radio switches, airplane mode | rfkill |
| Firmware: sticky Fn key, BIOS brightness presets | `panasonic_laptop` driver sysfs |

Both modes save the previous state and restore it exactly. They can be combined in any order.

## Requirements

Python 3.9+ and GTK 3 bindings, plus the standard tools it calls (no pip packages):

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 x11-xserver-utils pulseaudio-utils
sudo apt install gpsd gpsd-clients     # optional: for the gpsd GPS source
sudo apt install gammastep             # optional: screen tint on Wayland (wlroots)
```

Your user should be in `video` (backlight) and `dialout` (direct serial GPS).

## Install

```sh
git clone https://github.com/W0LAB/toughbook-linux-hub.git
cd toughbook-linux-hub
./install.sh            # user files + udev rule (asks for sudo once)
./install.sh --user-only
./install.sh --uninstall
```

The udev rule (`data/70-tbhub.rules`) does two things:
1. Lets the `video` group write Panasonic firmware attributes (otherwise root-only).
2. Gives the logged-in user read access to the **hotkey** input devices only (not regular
   keyboards), so Fn/A-button presses can be seen. **Log out and back in afterwards.**

## Binding Fn+A3 to night mode

Run learn mode and press the key to see what it sends:

```sh
tbhub keys --learn          # press Fn+A3
```

On the FZ-55 mk2, the A-buttons arrive on the `Panasonic Laptop Support` device as ordinary key
codes (Fn+A1 = `KEY_MACRO1` 656, Fn+A3 = `KEY_MACRO3` 658), so they are bound by key code:

```sh
tbhub keys --bind "night toggle" --device "Panasonic Laptop Support" --key 658
```

Buttons the kernel has no keymap entry for show up as `KEY_UNKNOWN` with a raw scan code
instead. Bind those with `--scan 0x..` using the value learn mode prints.

You can also use **Hotkeys → Learn key… → Bind** in the GUI. The hotkey service (`tbhub keys`,
started at login) reloads bindings automatically. Actions: any `tbhub` subcommand
(`night toggle`, `concealed toggle`, `brightness +10`, `radio airplane toggle`, `gui`), or
`exec <shell command>`. If nothing prints in learn mode, try `tbhub keys --learn --all`.
If a key shows up as a normal key on the regular keyboard, bind it in
*XFCE Settings → Keyboard → Application Shortcuts* to `tbhub night toggle` instead.

## GPS source

The GPS page has a **Source** panel. You can also pass `--source` on the command line.

| Source | Behaviour |
|---|---|
| `auto` (default) | Uses gpsd if it is reachable *and has a receiver*. Otherwise reads the serial device directly, and switches back to gpsd if gpsd reappears. |
| `gpsd` | gpsd at `gpsd_host:gpsd_port` (default `127.0.0.1:2947`). Can be a remote machine. |
| `serial` | Reads `gps_device` at `gps_baud` (default `/dev/gps0`, 9600) directly. No gpsd needed. |

Direct serial decodes both u-blox **UBX** (NAV-PVT/NAV-SAT, preferred: true accuracy in metres)
and **NMEA** (RMC/GGA/GNS/GSA/GSV, for any other receiver). It needs the `dialout` group.

Two programs reading one serial port corrupt each other's data. tbhub therefore refuses to open a device
that gpsd is currently reading, and tells you to stop gpsd first:

```sh
sudo systemctl stop gpsd.socket gpsd      # stop gpsd (it restarts at boot or replug)
tbhub gps --source serial --save          # remember the choice
tbhub gps --source gpsd --host 10.0.0.5   # one-off: read a remote gpsd
tbhub gps --devices                       # list candidate serial devices
```

## CLI

```
tbhub                       # GUI   (tbhub gui --page gps)
tbhub night [on|off|toggle|status]
tbhub concealed [on|off|toggle|status]
tbhub reset                 # panic button: clear tint, leave modes, brightness ≥ 50%
tbhub brightness [40|+10|-10]
tbhub gps [-w] [--json] [--source auto|gpsd|serial] [--host H] [--port P]
          [--device DEV] [--baud N] [--save] [--devices]
tbhub radio [wifi|bt|wwan|all|airplane] [on|off|toggle]
tbhub status [--json]
tbhub pana sticky_key 1
tbhub keys [--learn|--list|--bind ACTION ...]
```

Settings live in `~/.config/tbhub/config.json` (night tint colour/level, concealed options) and
`~/.config/tbhub/keys.json`. Saved mode state lives in `~/.local/state/tbhub/state.json`.

## Notes / known limits

- **gpsd reports the wrong GPS date on this unit (2019-04-08).** This is a week-number rollover
  error. The receiver's own UBX NAV-PVT carries the correct date, so the fault is in how gpsd
  decodes it, not in the NEO-M8J firmware. Position is unaffected. tbhub warns when a source's
  date is more than a day off. Don't use gpsd's time from this receiver for chrony/NTP.
- **Keyboard backlight colour is not controlled.** PC Hub's night mode also switches the
  keyboard light. On the FZ-55 the keyboard colour and brightness are internal to the
  embedded controller. Linux exposes no control for them, and reaching them would need an
  out-of-tree kernel module (signed for Secure Boot). Use Fn+A4 (colour) and Fn+F12
  (brightness) instead. Both work on Linux because the firmware handles them.
- `eco_mode` and `cdpower` return I/O errors on the FZ-55 (not implemented in firmware), so they are hidden.
- The xrandr tint resets if the display reconfigures (dock/undock). Toggle the mode again to re-apply it.

## License

MIT. See [LICENSE](LICENSE).
