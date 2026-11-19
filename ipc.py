"""Session-bus protocol shared by the CLI, window and tray (no GTK imports)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from gi.repository import Gio, GLib

NAME = "io.github.earbridge.Service"
PATH = "/io/github/earbridge/Service"
HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "earbridge"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "earbridge"
XML = f"""<node><interface name="{NAME}">
  <method name="Command"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
  <signal name="Changed"><arg type="s"/></signal>
</interface></node>"""


def bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION)


def running(connection=None):
    return (connection or bus()).call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "NameHasOwner", GLib.Variant("(s)", (NAME,)), None,
        Gio.DBusCallFlags.NONE, 2000, None,
    ).unpack()[0]


def spawn(part):
    """Detach from terminals and from the window's lifetime."""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE_DIR / f"{part}.log").open("a") as log:
        return subprocess.Popen(
            [sys.executable, str(HERE / "earbridge.py"), part],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )


def ensure_service():
    connection = bus()
    if running(connection):
        return connection
    proc = spawn("_service")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if running(connection):
            return connection
        if proc.poll() not in (None, 0):
            break
        time.sleep(0.05)
    raise RuntimeError(f"Could not start Earbridge. See {STATE_DIR / '_service.log'}")


def decode(result):
    response = json.loads(result.unpack()[0])
    if "error" in response:
        raise RuntimeError(response["error"])
    return response.get("result")


def command(action, *, connection=None, **arguments):
    return decode((connection or bus()).call_sync(
        NAME, PATH, NAME, "Command",
        GLib.Variant("(s)", (json.dumps({"action": action, **arguments}),)),
        None, Gio.DBusCallFlags.NO_AUTO_START, 45000, None,
    ))


def command_async(connection, action, done, **arguments):
    def finish(conn, result):
        try:
            value = decode(conn.call_finish(result))
        except (GLib.Error, RuntimeError, ValueError) as error:
            done(None, str(error))
        else:
            done(value, None)
    connection.call(
        NAME, PATH, NAME, "Command",
        GLib.Variant("(s)", (json.dumps({"action": action, **arguments}),)),
        None, Gio.DBusCallFlags.NO_AUTO_START, 45000, None, finish,
    )


def subscribe(connection, changed):
    return connection.signal_subscribe(
        NAME, NAME, "Changed", PATH, None, Gio.DBusSignalFlags.NONE,
        lambda conn, sender, path, interface, signal, params: changed(json.loads(params.unpack()[0])),
    )


def watch_service(connection, vanished):
    return Gio.bus_watch_name_on_connection(
        connection, NAME, Gio.BusNameWatcherFlags.NONE, None,
        lambda *_: vanished(),
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    # Only the service writes these files, and it owns the bus name first.
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
