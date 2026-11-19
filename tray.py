"""StatusNotifierItem and D-Bus menu, independent of the GTK window.

Uses Gio directly so GTK3/AppIndicator isn't required alongside the GTK4 UI.
"""

import os
import signal
import sys
import time

from gi.repository import Gio, GLib

import ipc

ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
TRAY_NAME = "io.github.earbridge.Tray"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
PROPERTIES = {
    "Category": "s", "Id": "s", "Title": "s", "Status": "s", "WindowId": "i",
    "IconThemePath": "s", "Menu": "o", "ItemIsMenu": "b", "IconName": "s",
    "IconPixmap": "a(iiay)", "OverlayIconName": "s", "OverlayIconPixmap": "a(iiay)",
    "AttentionIconName": "s", "AttentionIconPixmap": "a(iiay)", "AttentionMovieName": "s",
    "ToolTip": "(sa(iiay)ss)",
}
ITEM_XML = '<node><interface name="' + ITEM + '">' + ''.join(
    f'<property name="{name}" type="{kind}" access="read"/>' for name, kind in PROPERTIES.items()
) + """
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""
MENU_XML = """<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>"""


def properties(**values):
    return {name.replace("_", "-"): GLib.Variant("b" if isinstance(value, bool) else "s", value)
            for name, value in values.items()}


class Tray:
    def __init__(self, connection, loop):
        self.bus = connection
        self.loop = loop
        self.state = {"active": False, "phones": [], "requests": []}
        self.error = None
        self.revision = 1
        self.activation_token = None
        self.bus.register_object(ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0], self.item_call, self.item_property, None)
        self.bus.register_object(MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0], self.menu_call, self.menu_property, None)
        ipc.subscribe(connection, self.changed)
        ipc.watch_service(connection, loop.quit)
        ipc.command_async(connection, "status", lambda state, error: self.changed(state) if state else None)
        self.watches = [Gio.bus_watch_name_on_connection(
            connection, watcher, Gio.BusNameWatcherFlags.NONE,
            self.watcher_appeared, lambda *_: None,
        ) for watcher in ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher")]

    def watcher_appeared(self, connection, name, owner):
        def registered(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as error:
                print(f"earbridge tray: {error.message}", file=sys.stderr, flush=True)
        connection.call(name, "/StatusNotifierWatcher", name, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (TRAY_NAME,)), None, Gio.DBusCallFlags.NONE, 3000, None, registered)

    def label(self):
        if self.state.get("requests"):
            return "Bluetooth approval pending"
        if self.error or self.state.get("error"):
            return "Earbridge needs attention"
        count = sum(p["connected"] for p in self.state.get("phones", []))
        return f"Reception on · {count} phone(s) connected" if self.state.get("active") else "Reception off"

    def status(self):
        return "NeedsAttention" if self.state.get("requests") or self.error or self.state.get("error") else "Active"

    def item_property(self, connection, sender, path, interface, prop):
        values = {
            "Category": "Hardware", "Id": "earbridge", "Title": "Earbridge",
            "Status": self.status(), "WindowId": 0, "IconThemePath": str(ipc.HERE),
            "Menu": MENU_PATH, "ItemIsMenu": False, "IconName": "earbridge",
            "AttentionIconName": "earbridge", "ToolTip": ("earbridge", [], "Earbridge", self.label()),
        }
        value = values.get(prop, [] if PROPERTIES[prop].startswith("a(") else "")
        return GLib.Variant(PROPERTIES[prop], value)

    def menu_property(self, connection, sender, path, interface, prop):
        values = {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"), "IconThemePath": ("as", [str(ipc.HERE)])}
        return GLib.Variant(*values[prop])

    def item_call(self, connection, sender, path, interface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            self.open_ui()
        elif method == "ProvideXdgActivationToken":
            self.activation_token = params.unpack()[0]
        invocation.return_value(None)

    def open_ui(self):
        # Pass a compositor activation token only to the next UI launch.
        if self.activation_token:
            os.environ["XDG_ACTIVATION_TOKEN"] = self.activation_token
        try:
            ipc.spawn("ui")
        finally:
            if self.activation_token:
                os.environ.pop("XDG_ACTIVATION_TOKEN", None)
                self.activation_token = None

    def rows(self):
        busy = self.state.get("busy", False)
        return {
            0: properties(children_display="submenu"),
            1: properties(label=self.label(), enabled=False),
            2: properties(label="Open Earbridge", enabled=True),
            3: properties(label="Disable reception" if self.state.get("active") else "Enable reception", enabled=not busy),
            4: properties(type="separator"),
            5: properties(label="Quit Earbridge", enabled=not busy),
        }

    def layout(self, item_id, depth, names):
        rows = self.rows()
        props = {key: value for key, value in rows[item_id].items() if not names or key in names}
        children = []
        if item_id == 0 and depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self.layout(i, depth - 1 if depth > 0 else -1, names)) for i in rows if i]
        return (item_id, props, children)

    def menu_call(self, connection, sender, path, interface, method, params, invocation):
        args = params.unpack()
        try:
            if method == "GetLayout":
                item_id, depth, names = args
                value = GLib.Variant("(u(ia{sv}av))", (self.revision, self.layout(item_id, depth, names)))
            elif method == "GetGroupProperties":
                ids, names = args
                value = GLib.Variant("(a(ia{sv}))", ([(i, {k: v for k, v in props.items() if not names or k in names}) for i, props in self.rows().items() if not ids or i in ids],))
            elif method == "GetProperty":
                value = GLib.Variant("(v)", (self.rows()[args[0]][args[1]],))
            elif method in ("Event", "EventGroup"):
                for item_id, event, data, timestamp in ([args] if method == "Event" else args[0]):
                    if event == "clicked":
                        self.clicked(item_id)
                value = GLib.Variant("(ai)", ([],)) if method == "EventGroup" else None
            elif method == "AboutToShow":
                value = GLib.Variant("(b)", (False,))
            else:
                value = GLib.Variant("(aiai)", ([], []))
            invocation.return_value(value)
        except (KeyError, ValueError) as error:
            invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidMenuItem", str(error))

    def clicked(self, item_id):
        if item_id == 2:
            self.open_ui()
        elif item_id in (3, 5) and not self.state.get("busy"):
            def finished(result, error):
                self.error = error
                self.changed(self.state)
            action = "quit" if item_id == 5 else ("stop" if self.state.get("active") else "start")
            ipc.command_async(self.bus, action, finished)

    def changed(self, state):
        self.state = state
        self.revision += 1
        self.bus.emit_signal(None, ITEM_PATH, ITEM, "NewToolTip", None)
        self.bus.emit_signal(None, ITEM_PATH, ITEM, "NewStatus", GLib.Variant("(s)", (self.status(),)))
        self.bus.emit_signal(None, MENU_PATH, MENU, "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))


def run():
    connection = ipc.bus()
    if not ipc.running(connection):
        return 0
    # A previous service's tray may still be exiting during a fast restart.
    deadline = time.monotonic() + 3
    while True:
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
            GLib.Variant("(su)", (TRAY_NAME, 4)), None, Gio.DBusCallFlags.NONE, 2000, None,
        ).unpack()[0]
        if result == 1:
            break
        if time.monotonic() >= deadline or not ipc.running(connection):
            return 0
        time.sleep(0.05)
    loop = GLib.MainLoop()
    tray = Tray(connection, loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: loop.quit() or GLib.SOURCE_REMOVE)
    loop.run()
    connection.flush_sync(None)
    return 0
