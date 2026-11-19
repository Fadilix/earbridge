"""Run with dbus-run-session -- python3 tests/update_integration.py.

Real installed CLI, updater, GTK UI and background processes; local Git remote
and fake Bluetooth/audio hardware. Never uses the live desktop session.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gi.repository import Gio, GLib
import ipc
from test_updater import Installation


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for update state")


def dbus(name, path, interface, method, signature=None, arguments=()):
    return ipc.bus().call_sync(name, path, interface, method,
                               GLib.Variant(signature, arguments) if signature else None,
                               None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None).unpack()


def owner(name):
    if not dbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", "(s)", (name,))[0]:
        return None
    return dbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner", "(s)", (name,))[0]


def suite():
    assert not ipc.running(), "Run this test with a fresh dbus-run-session"
    with tempfile.TemporaryDirectory(prefix="earbridge-update-integration-") as temporary:
        fixture = Installation(temporary, fake_hardware=True)
        fixture.env.update(XDG_RUNTIME_DIR=temporary, GSETTINGS_BACKEND="memory", GIO_USE_VFS="local",
                           GTK_A11Y="none", ADW_DISABLE_PORTAL="1")
        broadway, ui = None, None
        ui_name = "io.github.earbridge.Earbridge"
        try:
            fixture.cli("start")
            fixture.cli("output", "headphones")
            fixture.cli("connect", "00:00:00:00:00:02")
            fixture.cli("autostart", "on")
            autostart = Path(temporary) / "config/autostart/earbridge.desktop"
            original_autostart = autostart.read_text()
            if shutil.which("gtk4-broadwayd"):
                with (Path(temporary) / "broadway.log").open("w") as log:
                    broadway = subprocess.Popen(["gtk4-broadwayd", "--port=0", ":29"], env=fixture.env, stdout=log, stderr=log)
                fixture.env.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=":29")
                time.sleep(0.2)
                with (Path(temporary) / "ui.log").open("w") as log:
                    ui = subprocess.Popen([str(fixture.prefix / "bin/earbridge"), "ui"], env=fixture.env, stdout=log, stderr=log)
                wait_for(lambda: owner(ui_name))
            old_service, old_ui = owner(ipc.NAME), owner(ui_name)
            latest = fixture.publish_update()
            assert fixture.cli("update", "--check")["update_available"]
            assert owner(ipc.NAME) == old_service
            assert owner(ui_name) == old_ui
            result = fixture.cli("update")
            assert result["updated"], result
            assert result["latest"] == latest
            assert result["warnings"] == [], result
            assert fixture.info()["revision"] == latest
            assert owner(ipc.NAME) != old_service
            state = fixture.cli("status")
            assert state["active"]
            assert state["output"] == "headphones"
            assert all(phone["connected"] for phone in state["phones"])
            assert autostart.read_text() == original_autostart
            if old_ui:
                wait_for(lambda: owner(ui_name))
                assert owner(ui_name) != old_ui
                assert ui.wait(timeout=3) == 0
            print("PASS: installed updater restarts service and UI, reconnects phones, and preserves settings/autostart", flush=True)

            service_after_update = owner(ipc.NAME)
            assert not fixture.cli("update")["updated"]
            assert owner(ipc.NAME) == service_after_update
            print("PASS: an up-to-date installation leaves the running service untouched", flush=True)

            with (fixture.source / "service.py").open("a") as stream:
                stream.write('\nraise RuntimeError("test startup failure")\n')
            fixture.commit("broken service")
            failed = subprocess.run([str(fixture.prefix / "bin/earbridge"), "update"], env=fixture.env,
                                    capture_output=True, text=True, timeout=30)
            assert failed.returncode != 0, failed.stdout
            assert "restored the previous installation" in failed.stderr, failed.stderr
            assert fixture.info()["revision"] == latest
            state = fixture.cli("status")
            assert state["active"]
            assert all(phone["connected"] for phone in state["phones"])
            assert state["output"] == "headphones"
            if old_ui:
                wait_for(lambda: owner(ui_name))
            print("PASS: a broken new service rolls back to the previous version and resumes reception", flush=True)
        except Exception:
            for log in Path(temporary).rglob("*.log"):
                print(f"{log.name}:\n{log.read_text()}", file=sys.stderr)
            raise
        finally:
            if owner(ui_name):
                dbus(ui_name, "/io/github/earbridge/Earbridge", "org.gtk.Actions", "Activate", "(sava{sv})", ("quit", [], {}))
                wait_for(lambda: not owner(ui_name))
            fixture.cli("quit")
            for proc in (ui, broadway):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)


if __name__ == "__main__":
    suite()
