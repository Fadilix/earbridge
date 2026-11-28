"""Test service with fake hardware, a real session bus, notifications and tray."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gi.repository import Gio, GLib
import ipc
import service
from test_bridge import FakeBluez, FakeRouter

service.Bluez = lambda changed: FakeBluez()
service.audio.PhoneRouter = FakeRouter
service.audio.outputs = lambda: [("headphones", "Wired headphones"), ("hdmi", "HDMI")]
service.audio.playback = lambda sink: {
    "name": sink or "headphones", "description": {"hdmi": "HDMI"}.get(sink, "Wired headphones"),
    "kind": "HDMI / DisplayPort" if sink == "hdmi" else "Headphones",
    "icon": "video-display-symbolic" if sink == "hdmi" else "audio-headphones-symbolic",
    "port": "Headphones", "available": True, "default": sink is None, "muted": False,
    "streams": [{"address": "00:00:00:00:00:01", "codec": "aac", "muted": False}],
}

TEST = "io.github.earbridge.Test"
TEST_PATH = "/Test"
XML = f'''<node><interface name="{TEST}">
<method name="Request"><arg type="s" direction="in"/></method>
<method name="Cancel"/>
<method name="NotificationAction"><arg type="s" direction="in"/></method>
<method name="Inspect"><arg type="s" direction="out"/></method>
</interface></node>'''
NOTIFY_XML = '''<node><interface name="org.freedesktop.Notifications">
<method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/>
<arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
<arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
<arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
</interface></node>'''
WATCHER_XML = '''<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>'''

answers = []
notifications = []
registrations = []
notification_id = 0


def call(conn, sender, path, interface, method, params, invocation):
    global notification_id
    args = params.unpack()
    result = None
    if method == "Request":
        background.ask("/new", args[0], answers.append)
    elif method == "Cancel":
        background.ask(None, None, None)
    elif method == "NotificationAction":
        connection.emit_signal(None, service.NOTIFICATION_PATH, service.NOTIFICATIONS, "ActionInvoked", GLib.Variant("(us)", (notification_id, args[0])))
    elif method == "Inspect":
        result = GLib.Variant("(s)", (json.dumps({"answers": answers, "notifications": notifications, "registrations": registrations,
                                                "props": background.bridge.bt.props if background.bridge else None}),))
    elif method == "Notify":
        notification_id += 1
        notifications.append(args[4])
        result = GLib.Variant("(u)", (notification_id,))
    elif method == "RegisterStatusNotifierItem":
        registrations.append(args[0])
    invocation.return_value(result)


class TestService(service.Service):
    def __init__(self, bus, loop):
        global connection, background
        connection = bus
        background = self
        for name in (service.NOTIFICATIONS, "org.kde.StatusNotifierWatcher"):
            connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                                 GLib.Variant("(su)", (name, 4)), None, Gio.DBusCallFlags.NONE, 2000, None)
        super().__init__(connection, loop)
        for path, xml in ((TEST_PATH, XML), (service.NOTIFICATION_PATH, NOTIFY_XML), ("/StatusNotifierWatcher", WATCHER_XML)):
            connection.register_object(path, Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], call, None, None)


service.Service = TestService
sys.exit(service.run())
