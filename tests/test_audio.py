"""Routing must move Bluetooth audio without touching normal desktop streams."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import audio


class RoutingTests(unittest.TestCase):
    def test_default_output_moves_only_phone_streams(self):
        router = audio.PhoneRouter()
        router._proc = object()
        sinks = [{"name": "headphones", "index": 7}]
        streams = [
            {"index": 1, "sink": 2, "properties": {"device.api": "bluez5"}},
            {"index": 2, "sink": 2, "properties": {"application.name": "Browser"}},
            {"index": 3, "sink": 7, "properties": {"node.name": "bluez_input.phone"}},
        ]
        with patch.object(audio, "_pactl_json", side_effect=[sinks, streams]), patch.object(audio.subprocess, "run", return_value=SimpleNamespace(stdout="headphones\n")) as run:
            router.set_sink(None)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["pactl", "get-default-sink"],
            ["pactl", "move-sink-input", "1", "headphones"],
        ])

    def test_missing_saved_output_leaves_streams_in_place(self):
        router = audio.PhoneRouter()
        router._proc = object()
        router.sink = "unplugged-headphones"
        with patch.object(audio, "_pactl_json", side_effect=[[], [{"index": 1, "sink": 2, "properties": {"device.api": "bluez5"}}]]), patch.object(audio.subprocess, "run") as run:
            router.route()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
