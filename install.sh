#!/bin/sh
# Install Toughbook Hub.  Usage: ./install.sh [--user-only | --uninstall]
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
RULES=/etc/udev/rules.d/70-tbhub.rules

reload_udev() {
    sudo udevadm control --reload
    sudo udevadm trigger --action=add --subsystem-match=acpi --sysname-match='MAT0019:00' || true
    sudo udevadm trigger --action=change --subsystem-match=input || true
}

if [ "${1:-}" = "--uninstall" ]; then
    pkill -f 'tbhub keys' 2>/dev/null || true
    rm -f "$BIN/tbhub" "$APPS/tbhub.desktop" "$AUTOSTART/tbhub-keys.desktop"
    if [ -e "$RULES" ]; then sudo rm -f "$RULES" && reload_udev; fi
    echo "Uninstalled (config left in ~/.config/tbhub)."
    exit 0
fi

mkdir -p "$BIN" "$APPS" "$AUTOSTART"
ln -sf "$HERE/bin/tbhub" "$BIN/tbhub"
cp "$HERE/data/tbhub.desktop" "$APPS/"
cp "$HERE/data/tbhub-keys.desktop" "$AUTOSTART/"
echo "Installed: $BIN/tbhub, menu entry, hotkey service autostart."

if [ "${1:-}" != "--user-only" ]; then
    echo "Installing udev rule $RULES (needs sudo)..."
    sudo install -m 0644 "$HERE/data/70-tbhub.rules" "$RULES"
    reload_udev
    echo "udev rule installed."
fi

id -nG | grep -qw video || echo "NOTE: add yourself to 'video' for backlight/firmware access: sudo usermod -aG video $USER"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "NOTE: $BIN is not on your PATH." ;; esac

# (re)start the hotkey service for this session
pkill -f 'tbhub keys' 2>/dev/null || true
nohup "$BIN/tbhub" keys >/dev/null 2>&1 &
echo "Done. Next: tbhub keys --learn   (then press Fn+A3)"
