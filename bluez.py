"""BlueZ access over D-Bus: adapter state, phones, and a pairing agent."""

from gi.repository import Gio, GLib

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPS = "org.freedesktop.DBus.Properties"
A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"  # the phone sends audio
AGENT_PATH = "/org/earbridge/agent"

# Adapter properties we change while active and restore afterwards.
SAVED_PROPS = {"Powered": "b", "Discoverable": "b", "DiscoverableTimeout": "u", "Pairable": "b"}

AGENT_XML = """
<node><interface name="org.bluez.Agent1">
  <method name="Release"/>
  <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
  <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
  <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
  <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
  <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
  <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="Cancel"/>
</interface></node>
"""


class Bluez:
    def __init__(self, on_change):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.adapter = next(
            (p for p, ifs in self._objects().items() if ADAPTER in ifs), None
        )
        if self.adapter is None:
            raise RuntimeError("No Bluetooth adapter found")
        self._on_change = on_change
        self._pending_change = 0
        self._agent_id = 0
        self.bus.signal_subscribe(
            BLUEZ, None, None, None, None, Gio.DBusSignalFlags.NONE, self._signal
        )

    # --- reading -----------------------------------------------------------

    def _objects(self):
        return self.bus.call_sync(
            BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

    def adapter_props(self):
        return self._objects()[self.adapter][ADAPTER]

    def phones(self):
        """Paired devices that can send audio, as dicts sorted by name."""
        out = []
        for path, ifs in self._objects().items():
            d = ifs.get(DEVICE)
            if not d or d.get("Adapter") != self.adapter or not d.get("Paired"):
                continue
            if A2DP_SOURCE not in d.get("UUIDs", []) and d.get("Icon") != "phone":
                continue
            out.append({
                "path": path,
                "name": d.get("Alias") or d.get("Name") or d["Address"],
                "address": d["Address"],
                "connected": bool(d.get("Connected")),
            })
        return sorted(out, key=lambda p: p["name"].lower())

    def name_of(self, path):
        d = self._objects().get(path, {}).get(DEVICE, {})
        return d.get("Alias") or d.get("Name") or d.get("Address") or path

    def _signal(self, *_):
        # BlueZ emits bursts of signals; collapse them into one refresh.
        if not self._pending_change:
            self._pending_change = GLib.timeout_add(150, self._emit_change)

    def _emit_change(self):
        self._pending_change = 0
        self._on_change()
        return GLib.SOURCE_REMOVE

    # --- adapter state -----------------------------------------------------

    def set_adapter(self, prop, value):
        self.bus.call_sync(
            BLUEZ, self.adapter, PROPS, "Set",
            GLib.Variant("(ssv)", (ADAPTER, prop, GLib.Variant(SAVED_PROPS[prop], value))),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    def snapshot(self):
        props = self.adapter_props()
        return {k: props[k] for k in SAVED_PROPS}

    def restore(self, saved):
        # Visibility first, power last: an unpowered adapter rejects the others.
        for prop in ("Discoverable", "DiscoverableTimeout", "Pairable", "Powered"):
            if prop in saved:
                self.set_adapter(prop, saved[prop])

    # --- device actions ----------------------------------------------------

    def _device_call(self, path, method, params, done):
        def finish(bus, res):
            try:
                bus.call_finish(res)
                done(None)
            except GLib.Error as e:
                done(e.message)
        self.bus.call(BLUEZ, path, DEVICE, method, params, None,
                      Gio.DBusCallFlags.NONE, 30000, None, finish)

    def connect_audio(self, path, done):
        self._device_call(path, "ConnectProfile", GLib.Variant("(s)", (A2DP_SOURCE,)), done)

    def disconnect(self, path, done=lambda err: None):
        self._device_call(path, "Disconnect", None, done)

    # --- pairing agent -----------------------------------------------------

    def register_agent(self, ask, allowed):
        """ask(device_path, question, reply) shows a prompt; reply(bool) answers BlueZ.
        allowed is a set of device paths that may connect without asking."""
        bluez_owner = self.bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetNameOwner", GLib.Variant("(s)", (BLUEZ,)), None,
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

        def reject(inv):
            inv.return_dbus_error("org.bluez.Error.Rejected", "Rejected")

        def answer(inv, device=None):
            def reply(ok):
                if ok:
                    if device:
                        allowed.add(device)
                    inv.return_value(None)
                else:
                    reject(inv)
            return reply

        def call(conn, sender, path, iface, method, params, inv):
            if sender != bluez_owner:  # the agent object is visible to any process
                return reject(inv)
            args = params.unpack()
            if method == "RequestConfirmation":
                dev, passkey = args
                ask(dev, f"Pair with {self.name_of(dev)}?\n"
                         f"Check that your phone shows {passkey:06d}.", answer(inv, dev))
            elif method == "RequestAuthorization":
                dev = args[0]
                ask(dev, f"Pair with {self.name_of(dev)}?", answer(inv, dev))
            elif method == "AuthorizeService":
                dev = args[0]
                if dev in allowed:
                    inv.return_value(None)
                else:
                    ask(dev, f"Let {self.name_of(dev)} play audio on this PC?", answer(inv, dev))
            elif method in ("Release", "Cancel", "DisplayPinCode", "DisplayPasskey"):
                if method == "Cancel":
                    ask(None, None, None)  # close any open prompt
                inv.return_value(None)
            else:  # legacy PIN / passkey entry: not supported
                reject(inv)

        node = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
        self._agent_id = self.bus.register_object(AGENT_PATH, node.interfaces[0], call, None, None)
        for method, params in (("RegisterAgent", ("DisplayYesNo",)), ("RequestDefaultAgent", ())):
            self.bus.call_sync(
                BLUEZ, "/org/bluez", "org.bluez.AgentManager1", method,
                GLib.Variant("(o" + "s" * len(params) + ")", (AGENT_PATH, *params)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )

    def unregister_agent(self):
        if not self._agent_id:
            return
        try:
            self.bus.call_sync(
                BLUEZ, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                GLib.Variant("(o)", (AGENT_PATH,)), None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            pass  # BlueZ restarted or already dropped it
        self.bus.unregister_object(self._agent_id)
        self._agent_id = 0
