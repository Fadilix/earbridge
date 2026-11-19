"""Self-update against a local Git remote; never touches the real installation."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import updater

ROOT = Path(__file__).resolve().parents[1]


class Installation:
    def __init__(self, temporary, fake_hardware=False):
        self.root = Path(temporary)
        self.source = self.root / "source"
        self.remote = self.root / "remote.git"
        self.prefix = self.root / "installed copy"
        self.app = self.prefix / "share/earbridge"
        self.env = dict(os.environ, PREFIX=str(self.prefix), XDG_CONFIG_HOME=str(self.root / "config"),
                        XDG_STATE_HOME=str(self.root / "state"), GIT_TERMINAL_PROMPT="0")
        self.source.mkdir()
        for file in [*ROOT.glob("*.py"), ROOT / "install.sh", ROOT / "earbridge.svg"]:
            shutil.copy2(file, self.source / file.name)
        if fake_hardware:
            with (self.source / "service.py").open("a") as stream:
                stream.write(f'\nsys.path.insert(0, {str(ROOT / "tests")!r})\n'
                             'from test_bridge import FakeBluez, FakeRouter\n'
                             'Bluez = lambda changed: FakeBluez()\n'
                             'audio.PhoneRouter = FakeRouter\n'
                             'audio.outputs = lambda: [("headphones", "Wired headphones")]\n')
        self.git("init", "--initial-branch=main", str(self.source))
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote), cwd=self.source)
        self.commit("initial")
        subprocess.run(["sh", str(self.source / "install.sh")], env=self.env, check=True, capture_output=True)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("add", ".", cwd=self.source)
        self.git("-c", "user.name=Earbridge Tests", "-c", "user.email=tests@example.invalid", "commit", "-m", message, cwd=self.source)
        self.git("push", "origin", "main", cwd=self.source)
        return self.git("rev-parse", "HEAD", cwd=self.source)

    def publish_update(self):
        with (self.source / "earbridge.svg").open("a") as stream:
            stream.write("\n<!-- updated -->\n")
        return self.commit("new version")

    def info(self):
        return json.loads((self.app / "install.json").read_text())

    def cli(self, *arguments):
        result = subprocess.run([str(self.prefix / "bin/earbridge"), "--json", *arguments], env=self.env,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


class FakeRuntime:
    stopped = False

    def stop(self):
        self.stopped = True

    def stop_service(self):
        pass

    def resume(self):
        return []


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="earbridge-update-test-")
        self.addCleanup(temporary.cleanup)
        self.install = Installation(temporary.name)

    def test_check_and_current_version_never_stop_runtime(self):
        with patch.object(updater, "Runtime") as runtime:
            result = updater.update(self.install.app)
            self.assertFalse(result["updated"])
            self.assertFalse(result["update_available"])
            latest = self.install.publish_update()
            result = updater.update(self.install.app, check_only=True)
            self.assertTrue(result["update_available"])
            self.assertEqual(result["latest"], latest)
            runtime.assert_not_called()
        self.assertNotEqual(self.install.info()["revision"], latest)
        self.assertTrue(self.install.cli("update", "--check")["update_available"])

    def test_update_installed_copy_preserves_settings_and_source_checkout(self):
        latest = self.install.publish_update()
        settings = self.install.root / "config/earbridge/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"output":"headphones"}')
        (self.install.source / "local-notes").write_text("uncommitted local work")
        with patch.object(updater, "Runtime", return_value=FakeRuntime()):
            result = updater.update(self.install.app)
        self.assertTrue(result["updated"])
        self.assertEqual(self.install.info()["revision"], latest)
        self.assertEqual(self.install.info()["prefix"], str(self.install.prefix))
        self.assertIn("<!-- updated -->", (self.install.app / "earbridge.svg").read_text())
        self.assertEqual(settings.read_text(), '{"output":"headphones"}')
        self.assertEqual((self.install.source / "local-notes").read_text(), "uncommitted local work")
        self.assertFalse(self.install.cli("update", "--check")["update_available"])
        subprocess.run(["desktop-file-validate", str(self.install.prefix / "share/applications/earbridge.desktop")], check=True)

    def test_failed_download_keeps_old_installation_and_runtime(self):
        before = self.install.info()
        broken = {**before, "repository": str(self.install.root / "missing.git")}
        (self.install.app / "install.json").write_text(json.dumps(broken))
        with patch.object(updater, "Runtime") as runtime:
            with self.assertRaisesRegex(RuntimeError, "Could not fetch update"):
                updater.update(self.install.app)
            runtime.assert_not_called()
        self.assertEqual(self.install.info()["revision"], before["revision"])

    def test_failed_staging_never_stops_runtime(self):
        before = self.install.info()
        (self.install.source / "install.sh").write_text("#!/bin/sh\necho staging-failed >&2\nexit 1\n")
        self.install.commit("broken installer")
        with patch.object(updater, "Runtime") as runtime:
            with self.assertRaisesRegex(RuntimeError, "installation unchanged: staging-failed"):
                updater.update(self.install.app)
            runtime.assert_not_called()
        self.assertEqual(self.install.info(), before)

    def test_failed_restart_rolls_back_files_and_resumes_previous_version(self):
        before = self.install.info()
        self.install.publish_update()
        class FailingRestart(FakeRuntime):
            attempts = 0

            def resume(inner):
                inner.attempts += 1
                if inner.attempts == 1:
                    self.assertNotEqual(self.install.info()["revision"], before["revision"])
                    raise RuntimeError("new service failed")
                self.assertEqual(self.install.info(), before)
                return []
        runtime = FailingRestart()
        with patch.object(updater, "Runtime", return_value=runtime):
            with self.assertRaisesRegex(RuntimeError, "restored the previous installation: new service failed"):
                updater.update(self.install.app)
        self.assertEqual(runtime.attempts, 2)
        self.assertEqual(self.install.info(), before)
        self.assertTrue((self.install.prefix / "bin/earbridge").is_file())

    def test_concurrent_update_is_rejected(self):
        self.install.publish_update()
        lock = self.install.app.parent / ".earbridge-update.lock"
        with lock.open("a") as stream, patch.object(updater, "Runtime") as runtime:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                updater.update(self.install.app)
            runtime.assert_not_called()

    def test_partial_file_replacement_restores_every_original(self):
        before = self.install.info()
        self.install.publish_update()
        original_apply = updater.PreparedInstall.apply

        def fail_during_install(replacement):
            # The app directory swaps successfully, then the binary swap fails.
            replacement.entries[1]["new"].unlink()
            original_apply(replacement)

        with patch.object(updater, "Runtime", return_value=FakeRuntime()), patch.object(updater.PreparedInstall, "apply", fail_during_install):
            with self.assertRaisesRegex(RuntimeError, "restored the previous installation"):
                updater.update(self.install.app)
        self.assertEqual(self.install.info(), before)
        self.assertTrue((self.install.prefix / "bin/earbridge").is_file())

    def test_unregistered_source_is_not_modified(self):
        with self.assertRaisesRegex(RuntimeError, "Run ./install.sh once"):
            updater.update(self.install.source)


if __name__ == "__main__":
    unittest.main()
