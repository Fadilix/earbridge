#!/bin/sh
# Install Earbridge for the current user (default) or system-wide.
#   ./install.sh                         install to ~/.local
#   sudo PREFIX=/usr/local ./install.sh  install for all users
#   ./install.sh --uninstall             remove it again (same PREFIX)
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$PREFIX/share/earbridge"
BIN="$PREFIX/bin/earbridge"
DESKTOP="$PREFIX/share/applications/earbridge.desktop"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/earbridge.svg"

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$APP" "$BIN" "$DESKTOP" "$ICON"
    echo "Earbridge removed from $PREFIX"
    exit 0
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "Earbridge needs Linux (BlueZ + PipeWire)." >&2
    exit 1
fi

# Check runtime dependencies and say which Arch packages provide them.
missing=""
need() { command -v "$1" >/dev/null 2>&1 || missing="$missing $2"; }
need python3 python
need pactl libpulse
need setpriv util-linux
need bluetoothctl bluez-utils
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' \
    >/dev/null 2>&1 || missing="$missing python-gobject gtk4 libadwaita"
if [ -n "$missing" ]; then
    echo "Missing dependencies. Install them first:" >&2
    echo "  sudo pacman -S --needed$missing pipewire-audio bluez" >&2
    exit 1
fi

install -d "$APP" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
install -m 644 "$SRC/bluez.py" "$SRC/audio.py" "$SRC/earbridge.svg" "$APP/"
install -m 644 "$SRC/earbridge.svg" "$ICON"
install -m 755 "$SRC/earbridge.py" "$APP/"
ln -sf "$APP/earbridge.py" "$BIN"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Earbridge
Comment=Listen to your phone's audio through this PC
Exec=$BIN
Icon=$ICON
Categories=AudioVideo;Audio;
EOF

# Launchers that look the icon up by name need the hicolor cache refreshed.
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP")" >/dev/null 2>&1 || true

echo "Earbridge installed: run 'earbridge' or open it from your app launcher."
case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) echo "Note: $PREFIX/bin is not on your PATH." ;;
esac
