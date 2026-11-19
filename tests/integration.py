"""Run with: dbus-run-session -- python3 tests/integration.py.

Uses the real CLI, service, tray, notifications protocol and GTK window with
fake Bluetooth/audio hardware. Does not touch the desktop's session bus.
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


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for a condition")


def cli(*args, ok=True):
    proc = subprocess.run([sys.executable, str(ROOT / "earbridge.py"), "--json", *args], capture_output=True, text=True, timeout=12)
    if ok:
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    assert proc.returncode != 0, proc.stdout
    return proc.stderr


def dbus(name, path, interface, method, signature=None, arguments=()):
    return ipc.bus().call_sync(name, path, interface, method, GLib.Variant(signature, arguments) if signature else None,
                               None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None)


def test_call(method, signature=None, arguments=()):
    return dbus(ipc.NAME, "/Test", "io.github.earbridge.Test", method, signature, arguments)


def inspect():
    return json.loads(test_call("Inspect").unpack()[0])


def suite():
    assert not ipc.running(), "This test requires a fresh isolated session bus"
    with tempfile.TemporaryDirectory(prefix="earbridge-test-") as temporary:
        work = Path(temporary)
        os.environ.update(XDG_STATE_HOME=str(work / "state"), XDG_CONFIG_HOME=str(work / "config"),
                          XDG_RUNTIME_DIR=temporary, GSETTINGS_BACKEND="memory", NO_AT_BRIDGE="1",
                          GIO_USE_VFS="local", GTK_A11Y="none", ADW_DISABLE_PORTAL="1")
        ipc.STATE_DIR = work / "state/earbridge"
        ipc.CONFIG_DIR = work / "config/earbridge"
        assert cli("status")["running"] is False
        assert not ipc.running()
        log = (work / "service.log").open("w+")
        service = subprocess.Popen([sys.executable, str(ROOT / "tests/fake_service.py")], stdout=log, stderr=log)
        broadway = None
        ui = None
        try:
            wait_for(lambda: ipc.running())
            wait_for(lambda: cli("status").get("adapter") == "Test PC")
            cli("start")
            initial = cli("status")
            assert initial["active"]
            assert len(cli("phones")) == 2
            assert len(cli("outputs")) == 3
            cli("output", "headphones")
            assert cli("status")["output"] == "headphones"
            assert "Unknown output" in cli("output", "missing", ok=False)
            cli("connect", "00:00:00:00:00:02")
            assert cli("phones")[1]["connected"]
            wait_for(lambda: inspect()["registrations"] == ["io.github.earbridge.Tray"])
            props = dbus("io.github.earbridge.Tray", "/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll", "(s)", ("org.kde.StatusNotifierItem",)).unpack()[0]
            assert props["Status"] == "Active"
            assert props["Menu"] == "/MenuBar"
            layout = dbus("io.github.earbridge.Tray", "/MenuBar", "com.canonical.dbusmenu", "GetLayout", "(iias)", (0, -1, [])).unpack()
            assert layout[1][2][2][1]["label"] == "Disable reception", layout
            print("PASS: CLI actions, output selection, tray registration and menu layout", flush=True)

            test_call("Request", "(s)", ("Pair with New phone? Check that it shows 123456.",))
            pending = cli("requests")[0]
            wait_for(lambda: len(inspect()["notifications"]) == 1)
            assert "123456" in inspect()["notifications"][0]
            assert "no longer pending" in cli("approve", "invalid", ok=False)
            assert inspect()["answers"] == []
            cli("approve", pending["id"])
            assert inspect()["answers"] == [True]
            assert cli("requests") == []
            test_call("Request", "(s)", ("Let phone play audio?",))
            wait_for(lambda: len(inspect()["notifications"]) == 2)
            # Wait for the async Notify reply to be processed before signaling.
            time.sleep(0.1)
            test_call("NotificationAction", "(s)", ("deny",))
            wait_for(lambda: inspect()["answers"] == [True, False])
            test_call("Request", "(s)", ("First request",))
            old_token = cli("requests")[0]["id"]
            test_call("Request", "(s)", ("Replacement request",))
            assert "no longer pending" in cli("approve", old_token, ok=False)
            test_call("Cancel")
            assert cli("requests") == []
            print("PASS: pairing via CLI and notifications, stale approvals, replacement and cancellation", flush=True)

            if shutil.which("gtk4-broadwayd"):
                with (work / "broadway.log").open("w") as out:
                    broadway = subprocess.Popen(["gtk4-broadwayd", "--port=0", ":23"], stdout=out, stderr=out)
                os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=":23")
                time.sleep(0.3)
                assert broadway.poll() is None
                ready = work / "ui-ready"
                with (work / "ui.log").open("w") as out:
                    ui = subprocess.Popen([sys.executable, str(ROOT / "tests/ui_driver.py"), "close", str(ready)], stdout=out, stderr=out)
                wait_for(ready.exists)
                assert ui.wait(timeout=5) == 0, (work / "ui.log").read_text()
                assert cli("status")["active"]
                assert cli("status")["output"] == "hdmi"
                ready.unlink()
                test_call("Request", "(s)", ("Pending while window closes",))
                token = cli("requests")[0]["id"]
                with (work / "ui.log").open("a") as out:
                    ui = subprocess.Popen([sys.executable, str(ROOT / "tests/ui_driver.py"), "close", str(ready)], stdout=out, stderr=out)
                wait_for(ready.exists)
                assert ui.wait(timeout=5) == 0
                assert cli("requests")[0]["id"] == token
                cli("deny", token)
                ready.unlink()
                with (work / "ui.log").open("a") as out:
                    ui = subprocess.Popen([sys.executable, str(ROOT / "tests/ui_driver.py"), "wait", str(ready)], stdout=out, stderr=out)
                wait_for(ready.exists)
                ui.kill()
                ui.wait(timeout=3)
                assert cli("status")["active"]
                assert cli("phones")[1]["connected"]
                assert inspect()["registrations"] == ["io.github.earbridge.Tray"]
                print("PASS: real GTK window sync, close with a pending request, and SIGKILL leave service and tray running", flush=True)
            else:
                print("SKIP: GTK lifecycle test (gtk4-broadwayd unavailable)", flush=True)

            dbus("io.github.earbridge.Tray", "/MenuBar", "com.canonical.dbusmenu", "Event", "(isvu)", (3, "clicked", GLib.Variant("i", 0), 0))
            wait_for(lambda: not cli("status")["active"])
            assert inspect()["props"]["Powered"] is False
            assert cli("phones")[0]["connected"]
            assert not cli("phones")[1]["connected"]
            assert not (work / "state/earbridge/session.json").exists()
            cli("start")
            cli("quit")
            assert service.wait(timeout=5) == 0
            assert not ipc.running()
            assert cli("status")["running"] is False
            print("PASS: tray toggle, restoration, preserved existing connection, and full quit", flush=True)

            assert cli("autostart", "on")["autostart"]
            entry = work / "config/autostart/earbridge.desktop"
            subprocess.run(["desktop-file-validate", str(entry)], check=True)
            assert cli("autostart", "off")["autostart"] is False
            print("PASS: optional login startup on/off and valid desktop entry", flush=True)

            # Restart the real service lifecycle with fake hardware, then kill it.
            service = subprocess.Popen([sys.executable, str(ROOT / "tests/fake_service.py")], stdout=log, stderr=log)
            wait_for(lambda: ipc.running())
            wait_for(lambda: cli("status").get("adapter") == "Test PC")
            assert cli("status")["output"] == "hdmi"
            cli("start")
            service.kill()
            service.wait(timeout=3)
            wait_for(lambda: not ipc.running())
            assert cli("status")["recovery_pending"]
            assert (work / "state/earbridge/session.json").exists()
            service = subprocess.Popen([sys.executable, str(ROOT / "tests/fake_service.py")], stdout=log, stderr=log)
            wait_for(lambda: ipc.running())
            wait_for(lambda: not cli("status").get("recovery_pending"))
            assert not cli("status")["active"]
            assert inspect()["props"]["Powered"] is False
            wait_for(lambda: inspect()["registrations"] == ["io.github.earbridge.Tray"])
            cli("start")
            service.terminate()
            assert service.wait(timeout=5) == 0
            assert not (work / "state/earbridge/session.json").exists()
            print("PASS: output persists across restarts, SIGKILL journal recovery, and graceful SIGTERM cleanup", flush=True)
        except Exception:
            for path in (work / "service.log", work / "ui.log", work / "broadway.log", work / "state/earbridge/_tray.log"):
                if path.exists():
                    print(f"{path.name}:\n{path.read_text()}", file=sys.stderr)
            raise
        finally:
            for proc in (ui, broadway, service):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            log.close()


if __name__ == "__main__":
    suite()
