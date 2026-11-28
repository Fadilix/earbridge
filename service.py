"""Background owner of Bluetooth, audio routing and pairing approvals."""

import json
import signal
import subprocess
import sys
import uuid

from gi.repository import Gio, GLib

import audio
from bluez import Bluez
from bridge import Bridge
import ipc

NOTIFICATIONS = "org.freedesktop.Notifications"
NOTIFICATION_PATH = "/org/freedesktop/Notifications"


class Service:
    def __init__(self, connection, loop):
        self.bus = connection
        self.loop = loop
        self.bridge = None
        self.error = None
        self.pending = None
        self.notification = None
        self.quitting = False
        self.recovering = False
        self.scan_timeout = 0
        self.pairing = set()
        self.waiting = []
        self.config_file = ipc.CONFIG_DIR / "settings.json"
        self.session_file = ipc.STATE_DIR / "session.json"
        try:
            self.output = json.loads(self.config_file.read_text()).get("output")
            if self.output is not None and not isinstance(self.output, str):
                raise ValueError("output must be a string or null")
        except FileNotFoundError:
            self.output = None
        except (OSError, ValueError, AttributeError) as error:
            # Output preferences must not prevent Bluetooth crash recovery.
            self.output = None
            print(f"earbridge: could not read output preference: {error}", file=sys.stderr, flush=True)
        self.bus.register_object(ipc.PATH, Gio.DBusNodeInfo.new_for_xml(ipc.XML).interfaces[0], self.call, None, None)
        self.bus.signal_subscribe(
            NOTIFICATIONS, NOTIFICATIONS, "ActionInvoked", NOTIFICATION_PATH, None,
            Gio.DBusSignalFlags.NONE, self.notification_action,
        )
        self.tray = None
        GLib.idle_add(self.initialize)

    def initialize(self):
        try:
            bridge = self.ensure_bridge()
            if bridge.session:
                self.recovering = True
                bridge.stop(self.recovered)
        except Exception as error:
            self.error = str(error)
            print(f"earbridge: {error}", file=sys.stderr, flush=True)
        try:
            self.tray = ipc.spawn("_tray")
        except OSError as error:
            print(f"earbridge tray: {error}", file=sys.stderr, flush=True)
        self.changed()
        return GLib.SOURCE_REMOVE

    def recovered(self, error):
        self.recovering = False
        self.completed(error)
        waiting, self.waiting = self.waiting, []
        for arguments, done in waiting:
            try:
                self.dispatch(arguments, done)
            except Exception as problem:
                done(error=str(problem))

    def ensure_bridge(self):
        if self.bridge is None:
            bt = Bluez(self.changed)
            router = audio.PhoneRouter(self.changed)
            router.set_sink(self.output)
            self.bridge = Bridge(bt, router, self.session_file, self.ask, self.changed)
            self.error = None
        return self.bridge

    def status(self):
        phones = []
        nearby = []
        adapter = None
        if self.bridge:
            try:
                adapter = self.bridge.bt.adapter_props().get("Alias", "this PC")
                phones = self.bridge.bt.phones()
                if self.scan_timeout:
                    nearby = [dict(d, pairing=d["path"] in self.pairing) for d in self.bridge.bt.candidates()]
            except Exception as error:
                self.error = str(error)
        return {
            "running": True,
            "active": bool(self.bridge and self.bridge.active),
            "busy": bool(self.bridge and self.bridge.busy),
            "recovery_pending": self.session_file.exists() and not bool(self.bridge and self.bridge.active),
            "output": self.output,
            "adapter": adapter,
            "phones": phones,
            "requests": self.requests(),
            "error": self.error,
            # Added for the window; older clients ignore these keys.
            "playback": audio.playback(self.output),
            "scanning": bool(self.scan_timeout),
            "nearby": nearby,
        }

    def requests(self):
        return [{k: self.pending[k] for k in ("id", "device", "question")}] if self.pending else []

    def changed(self):
        self.bus.emit_signal(None, ipc.PATH, ipc.NAME, "Changed", GLib.Variant("(s)", (json.dumps(self.status()),)))

    def completed(self, error, done=lambda result, error: None, message="Done."):
        self.error = error
        self.changed()
        done(message if not error else None, error)

    def call(self, connection, sender, path, interface, method, params, invocation):
        def done(result=None, error=None):
            response = {"error": str(error)} if error else {"result": result}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(response),)))
        try:
            arguments = json.loads(params.unpack()[0])
            self.dispatch(arguments, done)
        except Exception as error:
            done(error=str(error))

    def dispatch(self, arguments, done):
        action = arguments["action"]
        if self.quitting:
            return done(error="Earbridge is shutting down")
        if self.recovering and action in ("start", "stop", "quit"):
            self.waiting.append((arguments, done))
            return
        if action == "status":
            return done(self.status())
        if action == "requests":
            return done(self.requests())
        if action == "outputs":
            return done([{"name": "default", "description": "System default"}] + [
                {"name": name, "description": description} for name, description in audio.outputs()
            ])
        if action in ("approve", "deny"):
            if not self.pending or self.pending["id"] != arguments["request_id"]:
                raise RuntimeError("That request is no longer pending; run 'earbridge requests'")
            self.answer(action == "approve")
            return done("Request approved." if action == "approve" else "Request denied.")
        if action == "output":
            output = arguments["output"]
            if output != "default" and output not in {name for name, _ in audio.outputs()}:
                raise RuntimeError("Unknown output; run 'earbridge outputs' for available names")
            output = None if output == "default" else output
            ipc.write_json(self.config_file, {"output": output})
            self.output = output
            if self.bridge:
                self.bridge.router.set_sink(output)
            self.changed()
            return done(f"Output: {output or 'System default'}")
        if action == "quit" and self.bridge is None and not self.session_file.exists():
            done("Earbridge stopped.")
            return self.exit_later()
        bridge = self.ensure_bridge()
        if action == "start":
            return bridge.start(lambda error: self.completed(error, done, "Reception enabled in the background."))
        if action in ("stop", "quit"):
            self.stop_scan()
            def stopped(error):
                self.completed(error, done, "Earbridge stopped." if action == "quit" else "Reception disabled; Bluetooth settings restored.")
                if action == "quit" and not error:
                    self.exit_later()
            return bridge.stop(stopped)
        if action == "phones":
            return done(bridge.bt.phones())
        if action == "nearby":
            return done(bridge.bt.candidates())
        if action == "scan":
            if not arguments.get("enabled", True):
                self.stop_scan()
                return done("Stopped looking for devices.")
            if not bridge.active:
                raise RuntimeError("Reception is off; run 'earbridge start' first")
            def scanning(error):
                if not error and not self.scan_timeout:
                    # Discovery drains battery and radio time: cap it.
                    self.scan_timeout = GLib.timeout_add_seconds(45, self.scan_expired)
                self.completed(error, done, "Looking for nearby devices for 45 seconds.")
            if self.scan_timeout:
                return done("Already looking for nearby devices.")
            return bridge.bt.start_discovery(scanning)
        if action == "pair":
            if not bridge.active:
                raise RuntimeError("Reception is off; run 'earbridge start' first")
            selector = arguments["device"].casefold()
            matches = [d for d in bridge.bt.candidates() if selector in (d["address"].casefold(), d["name"].casefold(), d["path"].casefold())]
            if len(matches) != 1:
                raise RuntimeError("Device not found nearby or name is ambiguous; run 'earbridge nearby'")
            device = matches[0]
            if device["path"] in self.pairing:
                return done(f"{device['name']}: already pairing.")
            self.pairing.add(device["path"])
            def connected(error):
                self.pairing.discard(device["path"])
                self.completed(error, done, f"{device['name']}: added and connected.")
            def paired(error):
                if error or not bridge.active:
                    self.pairing.discard(device["path"])
                    return self.completed(error, done, f"{device['name']}: paired.")
                bridge.allowed.add(device["path"])
                bridge.bt.connect_audio(device["path"], connected)
            bridge.bt.pair(device["path"], paired)
            self.changed()
            return
        if action == "media":
            command = arguments["command"]
            if command not in ("play", "pause", "next", "previous"):
                raise RuntimeError("Media command must be play, pause, next or previous")
            players = [p["player"] for p in bridge.bt.phones() if p.get("connected") and p.get("player")
                       and arguments.get("phone", "").casefold() in ("", p["address"].casefold(), p["name"].casefold())]
            if not players:
                raise RuntimeError("No connected device is sharing its media controls")
            return bridge.bt.media(players[0]["path"], command, lambda error: self.completed(error, done, f"Sent {command}."))
        if action in ("connect", "disconnect"):
            if bridge.busy:
                raise RuntimeError("Bluetooth is changing state; try again shortly")
            if not bridge.active:
                raise RuntimeError("Reception is off; run 'earbridge start' first")
            selector = arguments["phone"]
            matches = [p for p in bridge.bt.phones() if selector.casefold() in (p["address"].casefold(), p["name"].casefold(), p["path"].casefold())]
            if len(matches) != 1:
                raise RuntimeError("Phone not found or name is ambiguous; use an address from 'earbridge phones'")
            phone = matches[0]
            was_allowed = phone["path"] in bridge.allowed
            bridge.busy = True
            if action == "connect":
                bridge.allowed.add(phone["path"])
            def finished(error):
                bridge.busy = False
                if action == "disconnect" or (error and not was_allowed):
                    bridge.allowed.discard(phone["path"])
                self.completed(error, done, f"{phone['name']}: {action} completed.")
            method = bridge.bt.connect_audio if action == "connect" else bridge.bt.disconnect
            try:
                method(phone["path"], finished)
            except Exception:
                bridge.busy = False
                if not was_allowed:
                    bridge.allowed.discard(phone["path"])
                raise
            self.changed()
            return
        raise RuntimeError(f"Unknown command: {action}")

    def scan_expired(self):
        # Swap in a no-op source so stop_scan can remove it like any other.
        self.scan_timeout = GLib.idle_add(lambda: GLib.SOURCE_REMOVE)
        self.stop_scan()
        return GLib.SOURCE_REMOVE

    def stop_scan(self):
        if not self.scan_timeout:
            return
        GLib.source_remove(self.scan_timeout)
        self.scan_timeout = 0
        if self.bridge:
            try:
                self.bridge.bt.stop_discovery(lambda error: None)
            except Exception as error:
                print(f"earbridge: could not stop discovery: {error}", file=sys.stderr, flush=True)
        self.changed()

    def exit_later(self):
        self.quitting = True
        GLib.idle_add(self.loop.quit)

    def shutdown(self):
        # A signal is handled on the GLib loop, so pending Bluetooth calls can finish.
        if self.bridge and self.bridge.busy:
            GLib.timeout_add(100, self.shutdown)
            return GLib.SOURCE_REMOVE
        self.quitting = True
        if self.bridge:
            def stopped(error):
                if error:
                    print(f"earbridge shutdown: {error}; recovery journal retained", file=sys.stderr, flush=True)
                self.loop.quit()
            self.bridge.stop(stopped)
        else:
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def ask(self, device, question, reply):
        self.answer(False)
        if question is None:
            return
        token = uuid.uuid4().hex[:12]
        self.pending = {"id": token, "device": device, "question": question, "reply": reply}
        self.pending["timeout"] = GLib.timeout_add_seconds(60, self.expire, token)
        self.changed()
        self.notify(token, question)

    def expire(self, token):
        if self.pending and self.pending["id"] == token:
            self.pending["timeout"] = None
            self.answer(False)
        return GLib.SOURCE_REMOVE

    def answer(self, allowed):
        if not self.pending:
            return
        pending, self.pending = self.pending, None
        if pending["timeout"]:
            GLib.source_remove(pending["timeout"])
        if self.notification:
            self.close_notification(self.notification[0])
            self.notification = None
        pending["reply"](allowed)
        self.changed()

    def notify(self, token, question):
        def sent(connection, result):
            try:
                notification_id = connection.call_finish(result).unpack()[0]
                if self.pending and self.pending["id"] == token:
                    self.notification = (notification_id, token)
                else:
                    self.close_notification(notification_id)
            except GLib.Error as error:
                print(f"earbridge: notifications unavailable ({error.message}); use 'earbridge requests' or the UI", file=sys.stderr, flush=True)
        self.bus.call(
            NOTIFICATIONS, NOTIFICATION_PATH, NOTIFICATIONS, "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                "Earbridge", 0, str(ipc.HERE / "earbridge.svg"), "Bluetooth approval",
                GLib.markup_escape_text(question) + "\nReview requests with: earbridge requests",
                ["allow", "Allow", "deny", "Deny", "default", "Open Earbridge"],
                {"urgency": GLib.Variant("y", 2), "desktop-entry": GLib.Variant("s", "earbridge")}, 60000,
            )), None, Gio.DBusCallFlags.NONE, 3000, None, sent,
        )

    def close_notification(self, notification_id):
        def closed(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error:
                pass
        self.bus.call(NOTIFICATIONS, NOTIFICATION_PATH, NOTIFICATIONS, "CloseNotification",
                      GLib.Variant("(u)", (notification_id,)), None, Gio.DBusCallFlags.NONE, 3000, None, closed)

    def notification_action(self, connection, sender, path, interface, signal_name, params):
        notification_id, action = params.unpack()
        if not self.notification or not self.pending:
            return
        if self.notification != (notification_id, self.pending["id"]):
            return
        if action in ("allow", "deny"):
            self.answer(action == "allow")
        elif action == "default":
            ipc.spawn("ui")


def run():
    connection = ipc.bus()
    # Claim the name before touching Bluetooth or reading the recovery journal.
    result = connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
        GLib.Variant("(su)", (ipc.NAME, 4)), None, Gio.DBusCallFlags.NONE, 2000, None,
    ).unpack()[0]
    if result != 1:
        return 0
    loop = GLib.MainLoop()
    service = Service(connection, loop)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, service.shutdown)
    try:
        loop.run()
    finally:
        if service.tray and service.tray.poll() is None:
            service.tray.terminate()
            try:
                service.tray.wait(timeout=3)
            except subprocess.TimeoutExpired:
                service.tray.kill()
                service.tray.wait()
        connection.flush_sync(None)
    return 0
