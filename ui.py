"""Optional GTK window. All Bluetooth and audio work belongs to the service."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

import ipc  # noqa: E402


class Window(Adw.ApplicationWindow):
    def __init__(self, app, connection):
        super().__init__(application=app, title="Earbridge", default_width=440, default_height=560)
        self.bus = connection
        self.state = {}
        self.outputs = []
        self.phone_rows = []
        self.pending_token = None
        self.syncing = False
        self.last_error = None
        self.subscription = ipc.subscribe(connection, self.refresh)
        self.watch = ipc.watch_service(connection, self.service_gone)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self.header())
        view.set_content(self.toasts)
        self.set_content(view)
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        bridge = Adw.PreferencesGroup(description="Closing this window keeps Earbridge running in the background.")
        self.switch = Adw.SwitchRow(title="Receive phone audio")
        self.switch.connect("notify::active", self.on_toggle)
        bridge.add(self.switch)
        self.output_row = Adw.ComboRow(title="Play through")
        self.output_row.connect("notify::selected", self.on_output)
        bridge.add(self.output_row)
        page.add(bridge)
        self.approval = Adw.PreferencesGroup(title="Bluetooth approval", description="Review the request before allowing it. Requests expire after 60 seconds.")
        self.approval_row = Adw.ActionRow(title="Phone request")
        self.approval_row.set_subtitle_lines(0)
        actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for label, action in (("Deny", "deny"), ("Allow", "approve")):
            button = Gtk.Button(label=label)
            if action == "approve":
                button.add_css_class("suggested-action")
            button.connect("clicked", self.on_approval, action)
            actions.append(button)
        self.approval_row.add_suffix(actions)
        self.approval.add(self.approval_row)
        self.approval.set_visible(False)
        page.add(self.approval)
        self.phones = Adw.PreferencesGroup(title="Phones")
        page.add(self.phones)
        self.connect("close-request", self.on_close)
        self.request("status", callback=self.refresh)
        self.load_outputs()
        # Refresh outputs when returning to the window after devices change.
        self.connect("notify::is-active", lambda *_: self.load_outputs() if self.is_active() else None)

    def header(self):
        logo = Gtk.Image.new_from_file(str(ipc.HERE / "earbridge.svg"))
        logo.set_pixel_size(22)
        title = Gtk.Box(spacing=8)
        title.append(logo)
        title.append(Gtk.Label(label="Earbridge", css_classes=["heading"]))
        bar = Adw.HeaderBar()
        bar.set_title_widget(title)
        return bar

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

    def refresh(self, state):
        self.state = state
        self.syncing = True
        active = state["active"]
        self.switch.set_active(active)
        self.switch.set_sensitive(not state.get("busy"))
        name = state.get("adapter") or "this PC"
        self.switch.set_subtitle(f"Visible as “{name}”. Pair or connect from your phone." if active else "Off. Bluetooth works as usual.")
        if state.get("recovery_pending"):
            self.switch.set_subtitle("Previous Bluetooth settings need restoration. Run ‘earbridge stop’ to retry.")
        self.select_output()
        self.syncing = False
        error = state.get("error")
        if error and error != self.last_error:
            self.toast(error)
        self.last_error = error

        for row in self.phone_rows:
            self.phones.remove(row)
        self.phone_rows = []
        if not state["phones"]:
            self.phone_rows.append(Adw.ActionRow(title="No paired phones yet", subtitle="Turn on reception, then pick this PC on your phone."))
        for phone in state["phones"]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(phone["name"]), subtitle=("Connected" if phone["connected"] else "Not connected") + f" · {phone['address']}")
            toggle = Gtk.Switch(active=phone["connected"], valign=Gtk.Align.CENTER, sensitive=active and not state.get("busy"))
            toggle.connect("state-set", self.on_phone_toggle, phone["address"])
            row.add_suffix(toggle)
            row.set_activatable_widget(toggle)
            self.phone_rows.append(row)
        for row in self.phone_rows:
            self.phones.add(row)
        self.sync_prompt(state.get("requests", []))

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
        for row in self.phone_rows:
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
