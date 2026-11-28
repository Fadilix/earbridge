"""Optional GTK window. All Bluetooth and audio work belongs to the service."""

import json

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import ipc  # noqa: E402

TAGLINE = "One pair of headphones. Every device."
DEVICE_ICONS = {"phone": "phone-symbolic", "computer": "computer-symbolic"}

CSS = """
.route { padding: 22px 16px 18px 16px; }
.node { border-radius: 999px;
        background: alpha(@window_fg_color, 0.06); color: alpha(@window_fg_color, 0.45); }
.node.on { background: alpha(@accent_bg_color, 0.16); color: @accent_color; }
.node.live { background: @accent_bg_color; color: @accent_fg_color; }
.node.small.on, .node.small.live { background: alpha(@accent_bg_color, 0.16); color: @accent_color; }
.node-name { font-weight: 700; }
.node-detail { font-size: 0.85em; opacity: 0.65; }
.flow { margin-top: 26px; }
.flow label { font-size: 1.4em; font-weight: 900; opacity: 0.18; }
.flow.on label { opacity: 0.45; color: @accent_color; }
.flow.live label { color: @accent_color; animation: earbridge-pulse 1.2s ease-in-out infinite; }
.flow.live label:nth-child(2) { animation-delay: 0.2s; }
.flow.live label:nth-child(3) { animation-delay: 0.4s; }
@keyframes earbridge-pulse { 0% { opacity: 0.15; } 50% { opacity: 1; } 100% { opacity: 0.15; } }
.headline { font-size: 1.25em; font-weight: 800; }
.tagline { font-size: 0.8em; opacity: 0.6; }
.np-title { font-size: 1.1em; font-weight: 700; }
.np-artist { opacity: 0.7; }
.np-app { font-size: 0.85em; opacity: 0.6; }
.np { padding: 14px 16px; }
"""


def device_icon(device):
    return DEVICE_ICONS.get(device.get("icon"), "phone-symbolic")


def device_summary(device, active):
    parts = []
    player = device.get("player") or {}
    if device.get("connected"):
        if active and (device.get("streaming") or player.get("status") == "playing"):
            parts.append("Playing")
        else:
            parts.append("Connected")
    else:
        parts.append("Not connected")
    if device.get("battery") is not None:
        parts.append(f"{device['battery']}% battery")
    return " · ".join(parts)


def source_of(state):
    """The device phone audio currently comes from: streaming first, then playing, then connected."""
    connected = [p for p in state.get("phones", []) if p.get("connected")]
    for test in (lambda p: p.get("streaming"), lambda p: (p.get("player") or {}).get("status") == "playing", lambda p: True):
        match = next((p for p in connected if test(p)), None)
        if match:
            return match, len(connected)
    return None, 0


class Node(Gtk.Box):
    """One stop on the audio route: a round icon with a name and a detail line."""

    def __init__(self, icon, small=False):
        # The PC is only a waypoint: keep it smaller so source and output stand out.
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=not small, width_request=80 if small else 120)
        size = 44 if small else 64
        self.badge = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, width_request=size, height_request=size,
                             margin_top=(64 - size) // 2, margin_bottom=(64 - size) // 2, css_classes=["node"] + (["small"] if small else []))
        self.icon = Gtk.Image(icon_name=icon, pixel_size=20 if small else 30, hexpand=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.badge.append(self.icon)
        self.name = Gtk.Label(css_classes=["node-name"] if not small else ["node-detail"], wrap=True, lines=2, ellipsize=3,
                              max_width_chars=14, justify=Gtk.Justification.CENTER)
        self.detail = Gtk.Label(css_classes=["node-detail"], ellipsize=3, max_width_chars=16, justify=Gtk.Justification.CENTER)
        for child in (self.badge, self.name, self.detail):
            self.append(child)

    def show(self, icon, name, detail, level):
        self.icon.set_from_icon_name(icon)
        self.name.set_label(name)
        self.name.set_tooltip_text(name)
        self.detail.set_label(detail)
        self.detail.set_tooltip_text(detail)
        for css in ("on", "live"):
            (self.badge.add_css_class if level in (css, "live" if css == "on" else None) else self.badge.remove_css_class)(css)


class Flow(Gtk.Box):
    """Three dots between two nodes; they pulse while audio is moving."""

    def __init__(self):
        super().__init__(spacing=2, valign=Gtk.Align.START, css_classes=["flow"])
        for _ in range(3):
            self.append(Gtk.Label(label="•"))

    def show(self, level):
        for css in ("on", "live"):
            (self.add_css_class if level in (css, "live" if css == "on" else None) else self.remove_css_class)(css)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, connection):
        super().__init__(application=app, title="Earbridge", default_width=480, default_height=760)
        self.bus = connection
        self.state = {}
        self.outputs = []
        self.phone_rows = []
        self.nearby_rows = []
        self.phones_key = None
        self.nearby_key = None
        self.pending_token = None
        self.syncing = False
        self.last_error = None
        self.subscription = ipc.subscribe(connection, self.refresh)
        self.watch = ipc.watch_service(connection, self.service_gone)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self.header())
        view.set_content(self.toasts)
        self.set_content(view)
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        page.add(self.route_group())
        page.add(self.now_playing_group())

        self.approval = Adw.PreferencesGroup(title="Approve this device?", description="Compare the code, then decide. Requests expire after 60 seconds.")
        self.approval_row = Adw.ActionRow(title="Bluetooth request", icon_name="bluetooth-symbolic")
        self.approval_row.set_subtitle_lines(0)
        actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for label, action in (("Deny", "deny"), ("Allow", "approve")):
            button = Gtk.Button(label=label)
            button.add_css_class("suggested-action" if action == "approve" else "destructive-action")
            button.connect("clicked", self.on_approval, action)
            actions.append(button)
        self.approval_row.add_suffix(actions)
        self.approval.add(self.approval_row)
        self.approval.set_visible(False)
        page.add(self.approval)

        bridge = Adw.PreferencesGroup(title="Reception", description="Closing this window keeps Earbridge running in the background.")
        self.switch = Adw.SwitchRow(title="Receive audio from your devices", icon_name="bluetooth-symbolic")
        self.switch.connect("notify::active", self.on_toggle)
        bridge.add(self.switch)
        self.output_row = Adw.ComboRow(title="Play through", icon_name="audio-headphones-symbolic")
        self.output_row.connect("notify::selected", self.on_output)
        bridge.add(self.output_row)
        page.add(bridge)

        self.phones = Adw.PreferencesGroup(title="Your devices", description="Paired phones, tablets and computers that can play through this PC.")
        page.add(self.phones)

        self.nearby = Adw.PreferencesGroup(title="Add a device", description="Find a nearby phone, tablet or laptop and pair it from here.")
        search = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(visible=False)
        self.search_button = Gtk.Button(label="Search", css_classes=["flat"])
        self.search_button.connect("clicked", self.on_search)
        search.append(self.spinner)
        search.append(self.search_button)
        self.nearby.set_header_suffix(search)
        page.add(self.nearby)

        self.connect("close-request", self.on_close)
        self.request("status", callback=self.refresh)
        self.load_outputs()
        # Refresh outputs when returning to the window after devices change.
        self.connect("notify::is-active", lambda *_: self.load_outputs() if self.is_active() else None)

    # --- layout ------------------------------------------------------------

    def header(self):
        logo = Gtk.Image.new_from_file(str(ipc.HERE / "earbridge.svg"))
        logo.set_pixel_size(26)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        words.append(Gtk.Label(label="Earbridge", css_classes=["heading"], xalign=0))
        words.append(Gtk.Label(label=TAGLINE, css_classes=["tagline"], xalign=0))
        title = Gtk.Box(spacing=10)
        title.append(logo)
        title.append(words)
        bar = Adw.HeaderBar()
        bar.set_title_widget(title)
        return bar

    def route_group(self):
        group = Adw.PreferencesGroup()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["card", "route"])
        route = Gtk.Box(spacing=4, homogeneous=False)
        self.source_node = Node("phone-symbolic")
        self.pc_node = Node("computer-symbolic", small=True)
        self.output_node = Node("audio-headphones-symbolic")
        self.flow_in = Flow()
        self.flow_out = Flow()
        for child in (self.source_node, self.flow_in, self.pc_node, self.flow_out, self.output_node):
            route.append(child)
        self.headline = Gtk.Label(css_classes=["headline"], wrap=True, justify=Gtk.Justification.CENTER)
        self.subline = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        # Put the main action where the state is shown, not only in the settings below.
        self.start_button = Gtk.Button(label="Start listening", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"], visible=False)
        self.start_button.connect("clicked", lambda *_: self.request("start"))
        card.append(route)
        card.append(self.headline)
        card.append(self.subline)
        card.append(self.start_button)
        group.add(card)
        return group

    def now_playing_group(self):
        self.np_group = Adw.PreferencesGroup(visible=False)
        card = Gtk.Box(spacing=12, css_classes=["card", "np"])
        art = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=36, css_classes=["dim-label"])
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER, spacing=2)
        self.np_app = Gtk.Label(xalign=0, css_classes=["np-app"], ellipsize=3)
        self.np_title = Gtk.Label(xalign=0, css_classes=["np-title"], ellipsize=3)
        self.np_artist = Gtk.Label(xalign=0, css_classes=["np-artist"], ellipsize=3)
        for child in (self.np_app, self.np_title, self.np_artist):
            words.append(child)
        controls = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.np_buttons = {}
        for command, icon in (("previous", "media-skip-backward-symbolic"), ("toggle", "media-playback-start-symbolic"), ("next", "media-skip-forward-symbolic")):
            button = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, css_classes=["circular"] + (["suggested-action"] if command == "toggle" else ["flat"]))
            button.connect("clicked", self.on_media, command)
            controls.append(button)
            self.np_buttons[command] = button
        self.np_buttons["previous"].set_tooltip_text("Previous")
        self.np_buttons["next"].set_tooltip_text("Next")
        for child in (art, words, controls):
            card.append(child)
        self.np_group.add(card)
        return self.np_group

    # --- service calls -----------------------------------------------------

    def request(self, action, callback=None, **arguments):
        def finished(result, error):
            if error:
                self.toast(error)
                if action != "status":
                    self.request("status", callback=self.refresh)
            elif callback:
                callback(result)
        ipc.command_async(self.bus, action, finished, **arguments)

    def load_outputs(self):
        def loaded(outputs):
            self.syncing = True
            self.outputs = outputs
            self.output_row.set_model(Gtk.StringList.new([o["description"] for o in outputs]))
            self.select_output()
            self.syncing = False
        self.request("outputs", callback=loaded)

    def select_output(self):
        selected = self.state.get("output") or "default"
        self.output_row.set_selected(next((i for i, o in enumerate(self.outputs) if o["name"] == selected), Gtk.INVALID_LIST_POSITION))

    def on_output(self, *_):
        if self.syncing:
            return
        idx = self.output_row.get_selected()
        if 0 <= idx < len(self.outputs):
            self.request("output", output=self.outputs[idx]["name"])

    def on_toggle(self, *_):
        if not self.syncing:
            self.request("start" if self.switch.get_active() else "stop")

    def on_media(self, button, command):
        source, _ = source_of(self.state)
        if not source or not source.get("player"):
            return
        if command == "toggle":
            command = "pause" if source["player"].get("status") == "playing" else "play"
        self.request("media", command=command, phone=source["address"])

    def on_search(self, *_):
        if not self.state.get("active"):
            # Searching needs reception; turn it on rather than refusing.
            self.request("start", callback=lambda _: self.request("scan", enabled=True))
            return
        self.request("scan", enabled=not self.state.get("scanning"))

    def on_pair(self, button, address):
        button.set_sensitive(False)
        button.set_label("Adding…")
        self.toast("Confirm the code on both screens to finish pairing.")
        self.request("pair", device=address)

    # --- state -------------------------------------------------------------

    def refresh(self, state):
        self.state = state
        self.syncing = True
        active = state["active"]
        busy = bool(state.get("busy"))
        self.switch.set_active(active)
        self.switch.set_sensitive(not busy)
        name = state.get("adapter") or "this PC"
        self.switch.set_subtitle(f"Visible as “{name}”. Pair or connect from any device." if active else "Off. Bluetooth works as usual.")
        if state.get("recovery_pending"):
            self.switch.set_subtitle("Previous Bluetooth settings need restoration. Run ‘earbridge stop’ to retry.")
        self.select_output()
        self.syncing = False
        error = state.get("error")
        if error and error != self.last_error:
            self.toast(error)
        self.last_error = error

        self.show_route(state)
        self.show_now_playing(state)
        self.show_phones(state)
        self.show_nearby(state)
        self.sync_prompt(state.get("requests", []))

    def show_route(self, state):
        active = state["active"]
        name = state.get("adapter") or "This PC"
        source, count = source_of(state) if active else (None, 0)
        playback = state.get("playback") or {}
        player = (source or {}).get("player") or {}
        live = bool(source and (source.get("streaming") or player.get("status") == "playing" or playback.get("streams")))

        if source:
            detail = player.get("app") or ("Playing" if live else "Connected")
            if source.get("battery") is not None:
                detail += f" · {source['battery']}%"
            self.source_node.show(device_icon(source), source["name"], detail, "live" if live else "on")
        elif active:
            self.source_node.show("bluetooth-symbolic", "Waiting…", "No device yet", "idle")
        else:
            self.source_node.show("bluetooth-disabled-symbolic", "No source", "Reception off", "idle")

        self.pc_node.show("computer-symbolic", name, "", "live" if live else ("on" if active else "idle"))
        self.pc_node.detail.set_visible(False)
        self.start_button.set_visible(not active and not state.get("busy"))

        out_name = playback.get("description") or "System default"
        out_detail = playback.get("kind") or "Output"
        if playback.get("available") is False:
            out_detail = "Unplugged"
        self.output_node.show(playback.get("icon") or "audio-headphones-symbolic", out_name, out_detail,
                              "live" if live else ("on" if active and playback.get("available", True) else "idle"))
        self.flow_in.show("live" if live else ("on" if source else "idle"))
        self.flow_out.show("live" if live else ("on" if active else "idle"))

        if not active:
            self.headline.set_label("Reception is off")
            self.subline.set_label("Start listening to hear your phone, tablet or laptop on these headphones. Your Bluetooth settings come back when you stop.")
        elif not source:
            self.headline.set_label("Ready for a device")
            self.subline.set_label(f"On your phone, tablet or laptop, open Bluetooth and pick “{name}”.")
        elif live:
            self.headline.set_label(f"Listening to {source['name']}")
            more = f" · {count - 1} more connected" if count > 1 else ""
            default = " (system default)" if playback.get("default") else ""
            self.subline.set_label(f"Playing on {out_name}{default}{more}")
        else:
            self.headline.set_label(f"{source['name']} is connected")
            self.subline.set_label(f"Play something on it to hear it on {out_name}.")

    def show_now_playing(self, state):
        source, _ = source_of(state) if state["active"] else (None, 0)
        player = (source or {}).get("player")
        visible = bool(player and (player.get("title") or player.get("status") in ("playing", "paused")))
        self.np_group.set_visible(visible)
        if not visible:
            return
        self.np_app.set_label(f"{player.get('app') or 'Media'} · {source['name']}")
        self.np_title.set_label(player.get("title") or "Unknown track")
        self.np_artist.set_label(player.get("artist") or player.get("album") or player.get("status", "").capitalize())
        self.np_artist.set_visible(bool(self.np_artist.get_label()))
        playing = player.get("status") == "playing"
        self.np_buttons["toggle"].set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self.np_buttons["toggle"].set_tooltip_text("Pause" if playing else "Play")

    def show_phones(self, state):
        active = state["active"]
        busy = bool(state.get("busy"))
        # Rebuild only on real changes, so toggles keep focus while tracks update.
        key = json.dumps([active, busy] + [[p["address"], p["name"], p.get("connected"), p.get("battery"), p.get("streaming"), p.get("icon"),
                                            (p.get("player") or {}).get("status")] for p in state["phones"]])
        if key == self.phones_key:
            return
        self.phones_key = key
        for row in self.phone_rows:
            self.phones.remove(row)
        self.phone_rows = []
        if not state["phones"]:
            self.phone_rows.append(Adw.ActionRow(title="No paired devices yet", subtitle="Turn on reception, then pick this PC on your device, or use Search below.", icon_name="phone-symbolic"))
        for phone in state["phones"]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(phone["name"]), subtitle=device_summary(phone, active), icon_name=device_icon(phone),
                                tooltip_text=phone["address"])
            toggle = Gtk.Switch(active=phone["connected"], valign=Gtk.Align.CENTER, sensitive=active and not busy)
            toggle.connect("state-set", self.on_phone_toggle, phone["address"])
            row.add_suffix(toggle)
            row.set_activatable_widget(toggle)
            self.phone_rows.append(row)
        for row in self.phone_rows:
            self.phones.add(row)

    def show_nearby(self, state):
        scanning = bool(state.get("scanning"))
        self.spinner.set_visible(scanning)
        self.search_button.set_label("Stop" if scanning else "Search")
        self.search_button.set_sensitive(not state.get("busy"))
        nearby = state.get("nearby", [])
        key = json.dumps([state["active"], scanning] + [[d["address"], d["name"], d.get("pairing")] for d in nearby])
        if key == self.nearby_key:
            return
        self.nearby_key = key
        for row in self.nearby_rows:
            self.nearby.remove(row)
        self.nearby_rows = []
        if not state["active"]:
            self.nearby_rows.append(Adw.ActionRow(title="Search for nearby devices", subtitle="Search turns reception on, then lists phones, tablets and laptops that aren’t paired yet.", icon_name="system-search-symbolic"))
        elif scanning and not nearby:
            self.nearby_rows.append(Adw.ActionRow(title="Looking for devices…", subtitle="Open Bluetooth settings on the device so it becomes visible.", icon_name="system-search-symbolic"))
        elif not nearby:
            self.nearby_rows.append(Adw.ActionRow(title="Search for nearby devices", subtitle="Phones, tablets and laptops that aren’t paired yet appear here.", icon_name="system-search-symbolic"))
        for device in nearby:
            signal = device.get("rssi")
            strength = "" if signal is None else (" · Very close" if signal > -60 else " · Nearby" if signal > -80 else " · Far away")
            row = Adw.ActionRow(title=GLib.markup_escape_text(device["name"]), subtitle=("Phone" if device.get("icon") == "phone" else "Computer or tablet") + strength,
                                icon_name=device_icon(device), tooltip_text=device["address"])
            button = Gtk.Button(label="Adding…" if device.get("pairing") else "Add", valign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"],
                                sensitive=not device.get("pairing"))
            button.connect("clicked", self.on_pair, device["address"])
            row.add_suffix(button)
            self.nearby_rows.append(row)
        for row in self.nearby_rows:
            self.nearby.add(row)

    def on_phone_toggle(self, toggle, state, address):
        self.request("connect" if state else "disconnect", phone=address)
        return True  # Wait for the service's confirmed state.

    def sync_prompt(self, requests):
        pending = requests[0] if requests else None
        self.pending_token = pending["id"] if pending else None
        self.approval.set_visible(pending is not None)
        if pending:
            self.approval_row.set_subtitle(GLib.markup_escape_text(pending["question"]))

    def on_approval(self, button, action):
        if self.pending_token:
            self.request(action, request_id=self.pending_token)

    def on_close(self, *_):
        # Closing/killing a client never stops reception or rejects a pending request.
        self.bus.signal_unsubscribe(self.subscription)
        Gio.bus_unwatch_name(self.watch)
        return False

    def service_gone(self):
        self.switch.set_sensitive(False)
        self.output_row.set_sensitive(False)
        self.search_button.set_sensitive(False)
        self.np_group.set_sensitive(False)
        for row in self.phone_rows + self.nearby_rows:
            row.set_sensitive(False)
        self.toast("The Earbridge service has stopped. Reopen the app to start it again.")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))


class App(Adw.Application):
    def __init__(self, connection):
        super().__init__(application_id="io.github.earbridge.Earbridge")
        self.connection = connection
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = Window(self, self.connection)
        win.present()


def run():
    return App(ipc.ensure_service()).run([str(ipc.HERE / "earbridge.py")])
