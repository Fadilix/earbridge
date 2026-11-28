"""Lifetime/recovery invariants. No real Bluetooth settings are changed."""

import json
from pathlib import Path
import tempfile
import unittest

from bridge import Bridge


class FakeBluez:
    adapter = "/org/bluez/hci0"

    def __init__(self):
        self.props = {"Powered": False, "Pairable": False, "Discoverable": False, "DiscoverableTimeout": 180}
        self.devices = [
            {"path": "/before", "name": "Existing phone", "address": "00:00:00:00:00:01", "connected": True,
             "icon": "phone", "battery": 64, "streaming": True,
             "player": {"path": "/before/player0", "app": "Music", "status": "playing", "title": "Night Drive",
                        "artist": "The Midnight", "album": "", "duration": 240000, "position": 61000}},
            {"path": "/new", "name": "New phone", "address": "00:00:00:00:00:02", "connected": False},
        ]
        self.nearby = [{"path": "/tablet", "name": "Living room tablet", "address": "00:00:00:00:00:03", "icon": "computer", "rssi": -48}]
        self.discovering = False
        self.media_commands = []
        self.disconnects = []
        self.pending_disconnects = []
        self.delayed = False
        self.fail_activation = False
        self.fail_restore = False
        self.registered = False

    def snapshot(self):
        return dict(self.props)

    def adapter_props(self):
        return {**self.props, "Alias": "Test PC"}

    def phones(self):
        return [dict(p) for p in self.devices]

    def set_adapter(self, prop, value):
        self.props[prop] = value

    def register_agent(self, ask, allowed):
        if self.fail_activation:
            raise RuntimeError("agent registration failed")
        self.ask = ask
        self.registered = True

    def unregister_agent(self):
        self.registered = False

    def restore(self, saved):
        if self.fail_restore:
            raise RuntimeError("restore failed")
        self.props = dict(saved)

    def disconnect(self, path, done):
        self.disconnects.append(path)
        if self.delayed:
            self.pending_disconnects.append(done)
        else:
            next(p for p in self.devices if p["path"] == path)["connected"] = False
            done(None)

    def connect_audio(self, path, done):
        next(p for p in self.devices if p["path"] == path)["connected"] = True
        done(None)

    def candidates(self):
        return [dict(d) for d in self.nearby]

    def start_discovery(self, done):
        self.discovering = True
        done(None)

    def stop_discovery(self, done=lambda err: None):
        self.discovering = False
        done(None)

    def pair(self, path, done):
        device = next(d for d in self.nearby if d["path"] == path)
        self.nearby.remove(device)
        self.devices.append({"path": path, "name": device["name"], "address": device["address"], "connected": False})
        done(None)

    def media(self, path, action, done):
        self.media_commands.append(action)
        done(None)


class FakeRouter:
    def __init__(self, changed=None):
        self.running = False
        self.sink = None

    def set_sink(self, sink):
        self.sink = sink

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.journal = Path(self.temporary.name) / "session.json"
        self.bt = FakeBluez()
        self.router = FakeRouter()
        self.bridge = self.make_bridge()

    def make_bridge(self):
        return Bridge(self.bt, self.router, self.journal, lambda *args: None, lambda: None)

    def test_stop_waits_for_disconnect_and_preserves_preexisting_connection(self):
        before = self.bt.snapshot()
        results = []
        self.bridge.start(results.append)
        self.assertEqual(results, [None])
        self.assertTrue(self.router.running)
        self.bt.devices[1]["connected"] = True
        self.bt.delayed = True
        self.bridge.stop(results.append)
        self.assertTrue(self.bridge.busy)
        self.assertTrue(self.journal.exists())
        self.assertTrue(self.bt.props["Powered"])
        self.assertEqual(self.bt.disconnects, ["/new"])
        self.bt.pending_disconnects.pop()(None)
        self.assertEqual(results, [None, None])
        self.assertEqual(self.bt.props, before)
        self.assertTrue(self.bt.devices[0]["connected"])
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.router.running)

    def test_activation_failure_rolls_back_adapter_and_journal(self):
        self.bt.fail_activation = True
        before = self.bt.snapshot()
        results = []
        self.bridge.start(results.append)
        self.assertIn("agent registration failed", results[0])
        self.assertEqual(self.bt.props, before)
        self.assertFalse(self.bridge.active)
        self.assertFalse(self.journal.exists())

    def test_failed_restore_keeps_journal_and_can_be_retried(self):
        self.bridge.start(lambda error: None)
        self.bt.fail_restore = True
        results = []
        self.bridge.stop(results.append)
        self.assertIn("restore failed", results[0])
        self.assertTrue(self.journal.exists())
        self.assertFalse(self.bridge.active)
        self.bt.fail_restore = False
        self.bridge.stop(results.append)
        self.assertIsNone(results[-1])
        self.assertFalse(self.journal.exists())

    def test_new_process_recovers_crash_journal(self):
        before = self.bt.snapshot()
        self.bridge.start(lambda error: None)
        self.bt.devices[1]["connected"] = True
        replacement = self.make_bridge()
        replacement.stop(lambda error: self.assertIsNone(error))
        self.assertEqual(self.bt.props, before)
        self.assertEqual(self.bt.disconnects, ["/new"])
        self.assertFalse(self.journal.exists())

    def test_wrong_adapter_and_corrupt_journals_are_preserved(self):
        self.journal.write_text(json.dumps({"adapter": self.bt.snapshot(), "adapter_path": "/other", "connected": []}))
        replacement = self.make_bridge()
        results = []
        replacement.stop(results.append)
        self.assertIn("another Bluetooth adapter", results[0])
        self.assertTrue(self.journal.exists())
        self.journal.write_text('{"adapter": 5}')
        with self.assertRaisesRegex(RuntimeError, "Invalid recovery journal"):
            self.make_bridge()


if __name__ == "__main__":
    unittest.main()
