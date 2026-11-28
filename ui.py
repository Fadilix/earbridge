import json

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, Gtk, WebKit

import ipc

PAGE = ipc.HERE / "web" / "index.html"
SETTINGS = ipc.CONFIG_DIR / "window.json"


def saved_theme():
    try:
        return json.loads(SETTINGS.read_text()).get("dark")
    except (OSError, ValueError, AttributeError):
        return None


class Window(Adw.ApplicationWindow):
    def __init__(self, app, connection):
        super().__init__(application=app, title="Earbridge", default_width=960, default_height=720)
        self.set_size_request(360, 480)
        self.bus = connection
        self.state = {}
        self.outputs = []
        self.ready = False
        dark = saved_theme()
        self.dark = Adw.StyleManager.get_default().get_dark() if dark is None else dark
        self.view = WebKit.WebView()
        self.view.set_background_color(self.color())
        manager = self.view.get_user_content_manager()
        manager.connect("script-message-received::earbridge", self.received)
        manager.register_script_message_handler("earbridge", None)
        self.view.connect("load-changed", self.loaded)
        self.view.load_uri(PAGE.as_uri())
        self.set_content(self.view)
        self.subscription = ipc.subscribe(connection, self.refresh)
        self.watch = ipc.watch_service(connection, lambda: self.run("serviceGone()"))
        self.connect("destroy", self.closed)
        self.connect("notify::is-active", lambda *_: self.load_outputs() if self.is_active() else None)
        self.request("status", self.refresh)
        self.load_outputs()

    def color(self):
        value = Gdk.RGBA()
        value.parse("#0e0e10" if self.dark else "#fbfbfc")
        return value

    def run(self, script):
        if self.ready:
            self.view.evaluate_javascript(script, -1, None, None, None, None, None)

    def loaded(self, view, event):
        if event == WebKit.LoadEvent.FINISHED:
            self.ready = True
            self.run(f"setTheme({json.dumps(self.dark)})")
            self.run(f"setOutputs({json.dumps(self.outputs)})")
            if self.state:
                self.run(f"update({json.dumps(self.state)})")

    def received(self, manager, value):
        message = json.loads(value.to_string())
        action = message.pop("action")
        if action == "move":
            seat = self.get_display().get_default_seat()
            self.get_surface().begin_move(seat.get_pointer(), 1, message["x"], message["y"], Gdk.CURRENT_TIME)
        elif action == "maximize":
            self.unmaximize() if self.is_maximized() else self.maximize()
        elif action == "close":
            self.close()
        elif action == "theme":
            self.dark = message["dark"]
            self.view.set_background_color(self.color())
            ipc.write_json(SETTINGS, {"dark": self.dark})
        elif action == "scan" and message.get("enabled") and not self.state.get("active"):
            self.request("start", lambda _: self.request("scan", enabled=True))
        else:
            self.request(action, **message)

    def request(self, action, callback=None, **arguments):
        def finished(result, error):
            if error:
                self.run(f"toast({json.dumps(error)})")
                if action != "status":
                    self.request("status", self.refresh)
            elif callback:
                callback(result)
        ipc.command_async(self.bus, action, finished, **arguments)

    def refresh(self, state):
        self.state = state
        self.run(f"update({json.dumps(state)})")

    def load_outputs(self):
        def loaded(outputs):
            self.outputs = outputs
            self.run(f"setOutputs({json.dumps(outputs)})")
        self.request("outputs", loaded)

    def select_output(self, name):
        self.request("output", output=name)

    def closed(self, *_):
        self.bus.signal_unsubscribe(self.subscription)
        Gio.bus_unwatch_name(self.watch)


class App(Adw.Application):
    def __init__(self, connection):
        super().__init__(application_id="io.github.earbridge.Earbridge")
        self.connection = connection

    def do_activate(self):
        win = self.get_active_window() or Window(self, self.connection)
        win.present()


def run():
    return App(ipc.ensure_service()).run([str(ipc.HERE / "earbridge.py")])
