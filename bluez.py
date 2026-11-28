"""BlueZ access over D-Bus: adapter state, phones, and a pairing agent."""

from gi.repository import Gio, GLib

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
PLAYER = "org.bluez.MediaPlayer1"
TRANSPORT = "org.bluez.MediaTransport1"
PLAYER_ACTIONS = {"play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous"}
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
        """Paired devices that can send audio, as dicts sorted by name.

        Also reports battery level, whether audio is streaming right now, and
        the phone's media player (AVRCP) when it exposes one."""
        objects = self._objects()
        out = []
        for path, ifs in objects.items():
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
                "icon": d.get("Icon") or "phone",
                "battery": ifs.get(BATTERY, {}).get("Percentage"),
                "streaming": False,
                "player": None,
            })
            phone = out[-1]
            for child, child_ifs in objects.items():
                if not child.startswith(path + "/"):
                    continue
                if child_ifs.get(TRANSPORT, {}).get("State") == "active":
                    phone["streaming"] = True
                player = child_ifs.get(PLAYER)
                if player and (phone["player"] is None or player.get("Status") == "playing"):
                    track = player.get("Track", {})
                    title = track.get("Title") or ""
                    phone["player"] = {
                        "path": child,
                        "app": player.get("Name") or "",
                        "status": player.get("Status") or "stopped",
                        "title": "" if title == "Not Provided" else title,
                        "artist": track.get("Artist") or "",
                        "album": track.get("Album") or "",
                        "duration": track.get("Duration") or 0,
                        "position": player.get("Position") or 0,
                    }
        return sorted(out, key=lambda p: p["name"].lower())

    def candidates(self):
        """Nearby, not yet paired devices that could send audio (phones, tablets, laptops)."""
        out = []
        for path, ifs in self._objects().items():
            d = ifs.get(DEVICE)
            if not d or d.get("Adapter") != self.adapter or d.get("Paired"):
                continue
            icon = d.get("Icon") or ""
            major = (d.get("Class", 0) >> 8) & 0x1F  # 1 = computer, 2 = phone
            if not (A2DP_SOURCE in d.get("UUIDs", []) or icon in ("phone", "computer") or major in (1, 2)):
                continue
            if not (d.get("Name") or d.get("Alias")):
                continue  # anonymous beacons are not worth listing
            out.append({
                "path": path,
                "name": d.get("Alias") or d.get("Name"),
                "address": d["Address"],
                "icon": icon or ("computer" if major == 1 else "phone"),
                "rssi": d.get("RSSI"),
            })
        # Strongest signal first: the device in your hand is usually closest.
        return sorted(out, key=lambda p: -(p["rssi"] if p["rssi"] is not None else -999))

    def _adapter_call(self, method, done):
        def finish(bus, res):
            try:
                bus.call_finish(res)
                done(None)
            except GLib.Error as e:
                done(e.message)
        self.bus.call(BLUEZ, self.adapter, ADAPTER, method, None, None,
                      Gio.DBusCallFlags.NONE, 10000, None, finish)

    def start_discovery(self, done):
        # BlueZ ends discovery by itself if this process exits.
        self._adapter_call("StartDiscovery", done)

    def stop_discovery(self, done=lambda err: None):
        self._adapter_call("StopDiscovery", done)

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
        if not self.adapter_props().get("Powered"):
            self.set_adapter("Powered", True)
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

    def pair(self, path, done):
        """Pair from this PC. BlueZ asks the agent to confirm the code."""
        def finish(bus, res):
            try:
                bus.call_finish(res)
                done(None)
            except GLib.Error as e:
                done(e.message)
        self.bus.call(BLUEZ, path, DEVICE, "Pair", None, None,
                      Gio.DBusCallFlags.NONE, 90000, None, finish)

    def connect_audio(self, path, done):
        self._device_call(path, "ConnectProfile", GLib.Variant("(s)", (A2DP_SOURCE,)), done)

    def disconnect(self, path, done=lambda err: None):
        self._device_call(path, "Disconnect", None, done)

    def media(self, player_path, action, done):
        """Send a play/pause/next/previous command to a phone's media player."""
        def finish(bus, res):
            try:
                bus.call_finish(res)
                done(None)
            except GLib.Error as e:
                done(e.message)
        self.bus.call(BLUEZ, player_path, PLAYER, PLAYER_ACTIONS[action], None, None,
                      Gio.DBusCallFlags.NONE, 10000, None, finish)

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
