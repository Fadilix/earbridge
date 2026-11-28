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


def _kind(sink):
    """A short label and a symbolic icon describing what the output is."""
    props = sink.get("properties", {})
    port = next((p for p in sink.get("ports", []) if p.get("name") == sink.get("active_port")), {})
    port_type = (port.get("type") or "").lower()
    form = props.get("device.form_factor", "")
    icon = props.get("device.icon_name", "")
    text = " ".join((port_type, form, icon, port.get("description") or "")).lower()
    bluetooth = props.get("device.api") == "bluez5"
    if "headset" in text:
        return ("Bluetooth headset" if bluetooth else "Headset"), "audio-headset-symbolic"
    if "headphone" in text:
        return ("Bluetooth headphones" if bluetooth else "Headphones"), "audio-headphones-symbolic"
    if "hdmi" in text or "display" in text:
        return "HDMI / DisplayPort", "video-display-symbolic"
    if "usb" in text or props.get("device.bus") == "usb":
        return "USB audio", "audio-card-symbolic"
    if bluetooth:
        return "Bluetooth speaker", "audio-speakers-symbolic"
    return "Speakers", "audio-speakers-symbolic"


def playback(sink_name):
    """Where phone audio plays and which phone streams are on it.

    sink_name None means the system default. Returns None when the audio
    server can't be reached, so status reporting never fails because of it."""
    try:
        default = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
        sinks = _pactl_json("list", "sinks")
        streams = _pactl_json("list", "sink-inputs")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    wanted = sink_name or default
    sink = next((s for s in sinks if s["name"] == wanted), None)
    if sink is None:
        return {"name": wanted, "description": "Not plugged in", "kind": "Unavailable",
                "icon": "audio-volume-muted-symbolic", "available": False, "default": sink_name is None,
                "streams": []}
    kind, icon = _kind(sink)
    port = next((p for p in sink.get("ports", []) if p.get("name") == sink.get("active_port")), {})
    phone_streams = []
    for stream in streams:
        if _is_phone_stream(stream) and stream.get("sink") == sink.get("index"):
            props = stream.get("properties", {})
            phone_streams.append({
                "address": str(props.get("api.bluez5.address") or props.get("device.string") or ""),
                "codec": str(props.get("api.bluez5.codec") or ""),
                "muted": bool(stream.get("mute")),
            })
    return {
        "name": sink["name"], "description": sink.get("description") or sink["name"],
        "kind": kind, "icon": icon, "port": port.get("description") or "",
        "available": port.get("availability") != "not available",
        "default": sink_name is None, "muted": bool(sink.get("mute")),
        "streams": phone_streams,
    }


def _is_phone_stream(stream):
    props = stream.get("properties", {})
    return props.get("device.api") == "bluez5" or str(props.get("node.name", "")).startswith("bluez_input.")


class PhoneRouter:
    """While running, keeps every incoming Bluetooth audio stream on one output.
    With no output set, incoming Bluetooth streams follow the system default."""

    def __init__(self, changed=lambda: None):
        self.sink = None
        self.changed = changed
        self._proc = None
        self._pending = 0

    def start(self):
        if self._proc:
            return
        # pdeathsig: don't leave pactl running if this app is killed.
        self._proc = Gio.Subprocess.new(["setpriv", "--pdeathsig", "TERM", "pactl", "subscribe"],
                                        Gio.SubprocessFlags.STDOUT_PIPE)
        self._read(Gio.DataInputStream.new(self._proc.get_stdout_pipe()))
        self.route()

    def stop(self):
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
        if self._proc:
            self._proc.force_exit()
            self._proc = None

    def set_sink(self, sink):
        self.sink = sink
        self.route()

    def _read(self, stream):
        process = self._proc
        def on_line(src, res):
            try:
                line, _ = src.read_line_finish_utf8(res)
            except GLib.Error:
                return
            if line is None or self._proc is not process:  # exited or stopped
                return
            if ("sink-input" in line or "'change' on sink" in line or "on server" in line) and not self._pending:
                self._pending = GLib.timeout_add(200, self._route_later)
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _route_later(self):
        self._pending = 0
        self.route()
        self.changed()  # streams or outputs changed: let clients redraw
        return GLib.SOURCE_REMOVE

    def route(self):
        if not self._proc:
            return
        try:
            sink = self.sink or subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True, text=True, check=True,
            ).stdout.strip()
            sinks = {s["name"]: s["index"] for s in _pactl_json("list", "sinks")}
            target = sinks.get(sink)
            for stream in _pactl_json("list", "sink-inputs"):
                if target is not None and _is_phone_stream(stream) and stream["sink"] != target:
                    subprocess.run(["pactl", "move-sink-input", str(stream["index"]), sink],
                                   capture_output=True)
        except (subprocess.CalledProcessError, ValueError, KeyError):
            pass  # audio server restarting; the next event retries
