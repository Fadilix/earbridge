#!/bin/sh
# Install Earbridge for the current user (default) or system-wide.
#   ./install.sh                         install to ~/.local
#   sudo PREFIX=/usr/local ./install.sh  install for all users
#   ./install.sh --uninstall             remove it again (same PREFIX)
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="${DESTDIR:-}$PREFIX"
APP="$INSTALL_ROOT/share/earbridge"
BIN="$INSTALL_ROOT/bin/earbridge"
DESKTOP="$INSTALL_ROOT/share/applications/earbridge.desktop"
ICON="$INSTALL_ROOT/share/icons/hicolor/scalable/apps/earbridge.svg"

if [ "${1:-}" = "--uninstall" ]; then
    if [ -z "${DESTDIR:-}" ] && [ -x "$BIN" ]; then
        "$BIN" quit >/dev/null 2>&1 || true
        "$BIN" autostart off >/dev/null 2>&1 || true
    fi
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
need git git
need pactl libpulse
need setpriv util-linux
need bluetoothctl bluez-utils
python3 -c 'import gi; gi.require_version("Gio","2.0"); from gi.repository import Gio' \
    >/dev/null 2>&1 || missing="$missing python-gobject"
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); gi.require_version("WebKit","6.0")' \
    >/dev/null 2>&1 || missing="$missing gtk4 libadwaita webkitgtk-6.0"
if [ -n "$missing" ]; then
    echo "Missing dependencies. Install them first:" >&2
    echo "  sudo pacman -S --needed$missing pipewire-audio bluez" >&2
    exit 1
fi

install -d "$APP" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
install -m 644 "$SRC/bluez.py" "$SRC/audio.py" "$SRC/bridge.py" "$SRC/ipc.py" \
    "$SRC/service.py" "$SRC/tray.py" "$SRC/ui.py" "$SRC/updater.py" "$SRC/earbridge.svg" "$APP/"
install -d "$APP/web"
install -m 644 "$SRC/web/index.html" "$APP/web/"
install -m 644 "$SRC/earbridge.svg" "$ICON"
install -m 755 "$SRC/earbridge.py" "$APP/"
ln -sf "$PREFIX/share/earbridge/earbridge.py" "$BIN"

# Desktop Exec escaping: quote the path and escape its reserved characters.
EXEC_PATH=$(python3 - "$SRC" "$PREFIX/bin/earbridge" "$PREFIX" "$APP" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from earbridge import desktop_exec
from updater import record_install
record_install(sys.argv[1], sys.argv[3], sys.argv[4])
print(desktop_exec(sys.argv[2]))
PY
)
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Earbridge
Comment=One pair of headphones. Every device.
Exec=$EXEC_PATH ui
Icon=$PREFIX/share/icons/hicolor/scalable/apps/earbridge.svg
Categories=AudioVideo;Audio;
Terminal=false
StartupNotify=true
EOF

# Launchers that look the icon up by name need the hicolor cache refreshed.
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP")" >/dev/null 2>&1 || true

echo "Earbridge installed: 'earbridge start' runs in the background; 'earbridge ui' opens the window."
echo "The Earbridge app launcher opens the UI. Run 'earbridge --help' for all commands."
case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) echo "Note: $PREFIX/bin is not on your PATH." ;;
esac
