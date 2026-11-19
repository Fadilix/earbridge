"""Exercise the actual GTK window on an isolated Broadway display."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gi.repository import GLib
import ipc
from ui import App
from gi.repository import Gtk

Gtk.init()
Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

mode, ready = sys.argv[1:]
app = App(ipc.ensure_service())
checks = 0


def check():
    global checks
    checks += 1
    window = app.get_active_window()
    if checks > 100:
        print("Window never synchronized", file=sys.stderr, flush=True)
        app.quit()
        return False
    if not window or not window.state.get("active") or len(window.outputs) != 3:
        return True
    if window.state.get("output") != "hdmi":
        window.output_row.set_selected(2)
        return True
    Path(ready).write_text("ready")
    if mode == "close":
        window.close()
    return False


GLib.timeout_add(100, check)
sys.exit(app.run(["earbridge-test-ui"]))
