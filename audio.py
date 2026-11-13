"""Audio outputs and routing of phone audio streams, via pactl (PipeWire)."""

import json
import subprocess

from gi.repository import Gio, GLib


def _pactl_json(*args):
    out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def outputs():
    """[(sink_name, description)] for every audio output."""
    return [(s["name"], s.get("description") or s["name"]) for s in _pactl_json("list", "sinks")]


def _is_phone_stream(stream):
    props = stream.get("properties", {})
    return props.get("device.api") == "bluez5" or str(props.get("node.name", "")).startswith("bluez_input.")


class PhoneRouter:
    """While running, keeps every incoming Bluetooth audio stream on one output.
    With no output set, streams follow the system default like normal."""

    def __init__(self):
        self.sink = None
        self._proc = None
        self._pending = 0

    def start(self):
        # pdeathsig: don't leave pactl running if this app is killed.
        self._proc = Gio.Subprocess.new(["setpriv", "--pdeathsig", "TERM", "pactl", "subscribe"],
                                        Gio.SubprocessFlags.STDOUT_PIPE)
        self._read(Gio.DataInputStream.new(self._proc.get_stdout_pipe()))
        self.route()

    def stop(self):
        if self._proc:
            self._proc.force_exit()
            self._proc = None

    def set_sink(self, sink):
        self.sink = sink
        self.route()

    def _read(self, stream):
        def on_line(src, res):
            try:
                line, _ = src.read_line_finish_utf8(res)
            except GLib.Error:
                return
            if line is None:  # pactl exited
                return
            if "sink-input" in line and not self._pending:
                self._pending = GLib.timeout_add(200, self._route_later)
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _route_later(self):
        self._pending = 0
        self.route()
        return GLib.SOURCE_REMOVE

    def route(self):
        if not self._proc or not self.sink:
            return
        try:
            sinks = {s["name"]: s["index"] for s in _pactl_json("list", "sinks")}
            target = sinks.get(self.sink)
            for stream in _pactl_json("list", "sink-inputs"):
                if target is not None and _is_phone_stream(stream) and stream["sink"] != target:
                    subprocess.run(["pactl", "move-sink-input", str(stream["index"]), self.sink],
                                   capture_output=True)
        except (subprocess.CalledProcessError, ValueError, KeyError):
            pass  # audio server restarting; the next event retries
