# toughbook-linux-hub (`tbhub`)

An unofficial Linux version of Panasonic's PC Hub for Toughbooks. Tested on an FZ-55 mk2
(Debian 13, XFCE/X11); other Toughbooks using the `panasonic_laptop` driver should work.

> Not affiliated with or endorsed by Panasonic. "Toughbook" and "Panasonic" are trademarks of
> Panasonic Corporation.

## Features

- **Night mode**: very dim screen with a red, green, amber or gray tint
- **Concealed mode**: lowest possible light, sound muted, optional radio silence
- **GPS**: position in MGRS, UTM, lat/lon and DMS, with accuracy and satellite signal
- **Hotkeys**: bind Fn / A-button combos (e.g. Fn+A3) to any action
- **Power**: charge, time left, battery health and cycles
- **Wireless**: per-radio switches and airplane mode
- **Firmware**: sticky Fn key, BIOS brightness presets

Both modes restore your previous settings when turned off, and can be combined.

## Install

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 pulseaudio-utils picom
git clone https://github.com/W0LAB/toughbook-linux-hub.git
cd toughbook-linux-hub
./install.sh
```

Then make sure your user is in the `video` and `dialout` groups, and **log out and back in**.

Optional: `gpsd gpsd-clients` for the gpsd GPS source, `gammastep` for the tint on Wayland.
`./install.sh --user-only` skips the udev rule; `./install.sh --uninstall` removes everything.

## Usage

Open **Toughbook Hub** from the menu, or use the command line:

```sh
tbhub night toggle          # also: on | off | status
tbhub night cycle           # next tint colour: red → green → amber → gray
tbhub concealed toggle
tbhub reset                 # panic button: clear tint, leave all modes, brightness ≥ 50%
tbhub brightness +10        # or 40, -10
tbhub gps -w                # live position
tbhub radio airplane toggle # or wifi | bt | wwan | all, on | off | toggle
tbhub status
```

Run `tbhub --help` or `tbhub <command> --help` for all options.

## Hotkeys

To bind Fn+A3 to night mode, press the key in learn mode, then bind the code it shows:

```sh
tbhub keys --learn          # press Fn+A3
tbhub keys --bind "night toggle" --device "Panasonic Laptop Support" --key 658
```

Or use **Hotkeys → Learn key… → Bind** in the GUI. Any `tbhub` command works as an action, as
does `exec <shell command>`.

- Keys shown as `KEY_UNKNOWN` are bound with `--scan 0x..` instead of `--key`.
- Nothing shows up in learn mode? Try `tbhub keys --learn --all`.
- Keys that arrive on the regular keyboard are better bound in
  *XFCE Settings → Keyboard → Application Shortcuts*.

## GPS

By default tbhub uses gpsd if it is running with a receiver, and otherwise reads the serial
port (`/dev/gps0`) directly. To pick a source:

```sh
tbhub gps --source serial --save          # always read the serial port
tbhub gps --source gpsd --host 10.0.0.5   # read a remote gpsd once
tbhub gps --devices                       # list serial GPS devices
```

Only one program can read the serial port, so stop gpsd before using `serial`:
`sudo systemctl stop gpsd.socket gpsd`.

## Configuration

Settings are saved from the GUI to `~/.config/tbhub/config.json` (modes, GPS) and
`~/.config/tbhub/keys.json` (hotkeys).

## Known limits

- **The keyboard backlight isn't changed by night mode.** Linux has no control for it on the
  FZ-55; use Fn+A4 (colour) and Fn+F12 (brightness).
- **gpsd shows the wrong date (2019) on the FZ-55's receiver.** Position is fine and tbhub warns
  about it, but don't use gpsd's time for NTP. Direct serial reads the correct date.
- **The screen flickers briefly when a mode turns on or off.** The tint uses picom, which
  takes over from XFCE's compositor only while a mode is on.
- **Docking or undocking clears the tint.** Toggle the mode again.

## License

MIT. See [LICENSE](LICENSE).
