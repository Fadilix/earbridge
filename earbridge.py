#!/usr/bin/env python3
"""Earbridge: play your phone's audio through this PC's speakers or headphones over Bluetooth.

Turning it on makes the PC visible and pairable, answers pairing requests, and
routes the phone's audio to the chosen output. Turning it off (or closing the
window) disconnects the phones it connected and restores Bluetooth as it was.
"""

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import audio  # noqa: E402
from bluez import Bluez  # noqa: E402

# Written while active so a crash or kill can still be undone on next launch.
SESSION_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "earbridge/session.json"


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Earbridge", default_width=440, default_height=560)
        self.bt = Bluez(self.refresh)
        self.router = audio.PhoneRouter()
        self.session = None      # saved adapter state + phones connected before activation
        self.allowed = set()     # phones allowed to connect during this session
        self.prompt = None       # (dialog, reply) currently on screen
        self.phone_rows = []
        self._syncing = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self.header())
        view.set_content(self.toasts)
        self.set_content(view)

        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        bridge = Adw.PreferencesGroup()
        self.switch = Adw.SwitchRow(title="Receive phone audio")
        self.switch.connect("notify::active", self.on_toggle)
        bridge.add(self.switch)
        self.output_row = Adw.ComboRow(title="Play through")
        self.output_row.connect("notify::selected", self.on_output)
        bridge.add(self.output_row)
        page.add(bridge)

        self.phones = Adw.PreferencesGroup(title="Phones")
        page.add(self.phones)

        self.connect("close-request", self.on_close)
        self.load_outputs()
        self.recover_crashed_session()
        self.refresh()

    def header(self):
        logo = Gtk.Image.new_from_file(os.path.join(HERE, "earbridge.svg"))
        logo.set_pixel_size(22)
        name = Gtk.Label(label="Earbridge", css_classes=["heading"])
        title = Gtk.Box(spacing=8)
        title.append(logo)
        title.append(name)
        bar = Adw.HeaderBar()
        bar.set_title_widget(title)
        return bar

    # --- outputs -----------------------------------------------------------

    def load_outputs(self):
        try:
            self.outputs = [(None, "System default")] + audio.outputs()
        except Exception as e:
            self.outputs = [(None, "System default")]
            self.toast(f"Could not list audio outputs: {e}")
        selected = self.output_row.get_selected() if hasattr(self, "outputs_model") else 0
        self.outputs_model = Gtk.StringList.new([d for _, d in self.outputs])
        self.output_row.set_model(self.outputs_model)
        self.output_row.set_selected(min(selected, len(self.outputs) - 1))

    def on_output(self, *_):
        idx = self.output_row.get_selected()
        if 0 <= idx < len(self.outputs):
            self.router.set_sink(self.outputs[idx][0])

    # --- activation --------------------------------------------------------

    def on_toggle(self, *_):
        if self._syncing:
            return
        try:
            self.activate_bridge() if self.switch.get_active() else self.deactivate_bridge()
        except GLib.Error as e:
            self.toast(f"Bluetooth error: {e.message}")
            self._set_switch(self.session is not None)
        self.refresh()

    def activate_bridge(self):
        if self.session:
            return
        self.session = {
            "adapter": self.bt.snapshot(),
            "connected": [p["path"] for p in self.bt.phones() if p["connected"]],
        }
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(self.session))
        try:
            self.bt.set_adapter("Powered", True)
            self.bt.set_adapter("Pairable", True)
            self.bt.set_adapter("DiscoverableTimeout", 0)
            self.bt.set_adapter("Discoverable", True)
            self.bt.register_agent(self.ask, self.allowed)
            self.load_outputs()
            self.router.start()
            self.on_output()
        except Exception:
            self.deactivate_bridge()
            raise

    def deactivate_bridge(self):
        if not self.session:
            return
        self.router.stop()
        self.ask(None, None, None)
        self.bt.unregister_agent()
        self.end_session(self.session)
        self.session = None
        self.allowed.clear()

    def end_session(self, session):
        before = set(session["connected"])
        for phone in self.bt.phones():
            if phone["connected"] and phone["path"] not in before:
                self.bt.disconnect(phone["path"])
        self.bt.restore(session["adapter"])
        SESSION_FILE.unlink(missing_ok=True)

    def recover_crashed_session(self):
        try:
            session = json.loads(SESSION_FILE.read_text())
        except (OSError, ValueError):
            return
        try:
            self.end_session(session)
            self.toast("Restored Bluetooth settings left over from the last run")
        except GLib.Error as e:
            self.toast(f"Could not restore previous Bluetooth settings: {e.message}")

    def on_close(self, *_):
        try:
            self.deactivate_bridge()
        except GLib.Error:
            pass  # session file stays, so the next launch restores it
        return False

    # --- pairing prompts ---------------------------------------------------

    def ask(self, device, question, reply):
        """Show a yes/no prompt for BlueZ. ask(None, None, None) dismisses it."""
        if self.prompt:
            dialog, old_reply = self.prompt
            self.prompt = None
            old_reply(False)
            dialog.force_close()
        if question is None:
            return
        dialog = Adw.AlertDialog(heading="Bluetooth", body=question)
        dialog.add_response("deny", "Deny")
        dialog.add_response("allow", "Allow")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("allow")
        dialog.set_close_response("deny")

        def on_response(d, response):
            if self.prompt and self.prompt[0] is d:
                self.prompt = None
                reply(response == "allow")
        dialog.connect("response", on_response)
        self.prompt = (dialog, reply)
        dialog.present(self)
        self.present()

    # --- view --------------------------------------------------------------

    def _set_switch(self, active):
        self._syncing = True
        self.switch.set_active(active)
        self._syncing = False

    def refresh(self):
        active = self.session is not None
        self._set_switch(active)
        try:
            name = self.bt.adapter_props().get("Alias", "this PC")
            phones = self.bt.phones()
        except GLib.Error as e:
            self.toast(f"Bluetooth unavailable: {e.message}")
            return
        self.switch.set_subtitle(
            f"Visible as “{name}”. Pair or connect from your phone's Bluetooth settings."
            if active else "Off. Bluetooth works as usual."
        )

        for row in self.phone_rows:
            self.phones.remove(row)
        self.phone_rows = []
        if not phones:
            self.phone_rows.append(Adw.ActionRow(
                title="No paired phones yet",
                subtitle="Turn on “Receive phone audio”, then pick this PC on your phone.",
            ))
        for phone in phones:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(phone["name"]),
                subtitle=("Connected" if phone["connected"] else "Not connected") + f" · {phone['address']}",
            )
            toggle = Gtk.Switch(active=phone["connected"], valign=Gtk.Align.CENTER, sensitive=active)
            toggle.connect("state-set", self.on_phone_toggle, phone["path"])
            row.add_suffix(toggle)
            row.set_activatable_widget(toggle)
            self.phone_rows.append(row)
        for row in self.phone_rows:
            self.phones.add(row)

    def on_phone_toggle(self, toggle, state, path):
        def done(err):
            if err:
                self.toast(err)
            self.refresh()
        if state:
            self.allowed.add(path)
            self.bt.connect_audio(path, done)
        else:
            self.bt.disconnect(path, done)
        return False

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.earbridge.Earbridge")

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            try:
                win = Window(self)
            except (RuntimeError, GLib.Error) as e:
                print(f"earbridge: {e}", file=sys.stderr)
                return
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
