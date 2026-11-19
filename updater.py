"""Update an installed copy from main, staging files before restarting anything."""

from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

UPSTREAM = "https://github.com/Fadilix/earbridge.git"
BRANCH = "main"
UI_NAME = "io.github.earbridge.Earbridge"
UI_PATH = "/io/github/earbridge/Earbridge"


def git(*arguments, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(["git", *arguments], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=120, check=True)
    except FileNotFoundError:
        raise RuntimeError("Updating requires git. Install git and try again.") from None
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Could not fetch update information: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired:
        raise RuntimeError("The update download timed out; the installation has not changed.") from None
    return result.stdout.strip()


def record_install(source, prefix, app):
    """Called by install.sh for normal and staged installations."""
    source, prefix, app = Path(source).resolve(), Path(prefix).absolute(), Path(app)
    revision, repository = None, UPSTREAM
    if (source / ".git").exists():
        try:
            revision = git("rev-parse", "HEAD", cwd=source)
            repository = git("remote", "get-url", "origin", cwd=source)
        except RuntimeError:
            pass
    if repository in ("git@github.com:Fadilix/earbridge.git", "ssh://git@github.com/Fadilix/earbridge.git"):
        repository = UPSTREAM
    (app / "install.json").write_text(json.dumps({
        "prefix": str(prefix), "repository": repository, "branch": BRANCH, "revision": revision,
    }) + "\n")


def wait_until(predicate, message, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(0.05)


class Runtime:
    """Snapshot and resume this desktop session; no bus is needed for an offline install."""

    def __init__(self):
        import ipc
        from gi.repository import GLib
        self.ipc = ipc
        self.state = None
        self.ui_open = False
        self.stopped = False
        try:
            self.bus = ipc.bus()
        except GLib.Error:
            self.bus = None
            return
        if ipc.running(self.bus):
            self.state = ipc.command("status", connection=self.bus)
            if self.state.get("busy") or self.state.get("requests"):
                raise RuntimeError("Finish the Bluetooth operation or pending pairing request before updating.")
        self.ui_open = self.has_name(UI_NAME)

    def dbus(self, name, path, interface, method, signature=None, arguments=()):
        from gi.repository import Gio, GLib
        return self.bus.call_sync(name, path, interface, method,
                                  GLib.Variant(signature, arguments) if signature else None,
                                  None, Gio.DBusCallFlags.NO_AUTO_START, 5000, None).unpack()

    def has_name(self, name):
        return self.dbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "NameHasOwner", "(s)", (name,))[0]

    def stop(self):
        # Set this before stopping so a partially stopped session can also resume.
        self.stopped = True
        if self.ui_open and self.has_name(UI_NAME):
            actions = self.dbus(UI_NAME, UI_PATH, "org.gtk.Actions", "List")[0]
            if "quit" in actions:
                self.dbus(UI_NAME, UI_PATH, "org.gtk.Actions", "Activate", "(sava{sv})", ("quit", [], {}))
            else:
                # Compatibility with versions before the UI's quit action existed.
                pid = self.dbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetConnectionUnixProcessID", "(s)", (UI_NAME,))[0]
                os.kill(pid, signal.SIGTERM)
            wait_until(lambda: not self.has_name(UI_NAME), "The Earbridge window did not close; no files were replaced.")
        self.stop_service()

    def stop_service(self):
        if self.bus and self.ipc.running(self.bus):
            self.ipc.command("quit", connection=self.bus)
            wait_until(lambda: not self.ipc.running(self.bus), "The Earbridge service did not stop.")

    def resume(self):
        warnings = []
        if self.state:
            connection = self.ipc.ensure_service()
            status = self.ipc.command("status", connection=connection)
            if status.get("error") and not self.state.get("error"):
                raise RuntimeError(f"The updated service could not initialize: {status['error']}")
            if self.state["active"]:
                self.ipc.command("start", connection=connection)
                connected = {p["address"] for p in self.ipc.command("phones", connection=connection) if p["connected"]}
                for phone in self.state["phones"]:
                    if phone["connected"] and phone["address"] not in connected:
                        try:
                            self.ipc.command("connect", connection=connection, phone=phone["address"])
                        except Exception as error:
                            warnings.append(f"Could not reconnect {phone['name']}: {error}")
        if self.ui_open and not self.has_name(UI_NAME):
            process = self.ipc.spawn("ui")
            try:
                wait_until(lambda: self.has_name(UI_NAME) or process.poll() is not None,
                           "The Earbridge window did not restart.")
                if not self.has_name(UI_NAME):
                    raise RuntimeError("The Earbridge window failed to start; see ui.log.")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                raise
        return warnings


class PreparedInstall:
    """Prepare replacements on each destination filesystem and retain rollback copies."""

    def __init__(self, staged_prefix, prefix, stack):
        self.entries = []
        for relative in ("share/earbridge", "bin/earbridge", "share/applications/earbridge.desktop",
                         "share/icons/hicolor/scalable/apps/earbridge.svg"):
            source, target = staged_prefix / relative, prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".earbridge-update-", dir=target.parent)))
            new, backup = temporary / "new", temporary / "old"
            if source.is_dir():
                shutil.copytree(source, new, symlinks=True)
            else:
                shutil.copy2(source, new, follow_symlinks=False)
            self.entries.append({"target": target, "new": new, "backup": backup, "saved": False, "installed": False})

    def apply(self):
        for entry in self.entries:
            target = entry["target"]
            if target.exists() or target.is_symlink():
                target.replace(entry["backup"])
                entry["saved"] = True
            entry["new"].replace(target)
            entry["installed"] = True

    def rollback(self):
        for entry in reversed(self.entries):
            if entry["installed"]:
                target = entry["target"]
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                entry["installed"] = False
            if entry["saved"]:
                entry["backup"].replace(entry["target"])
                entry["saved"] = False


def refresh_desktop(prefix):
    for command in (["gtk-update-icon-cache", "-qtf", str(prefix / "share/icons/hicolor")],
                    ["update-desktop-database", str(prefix / "share/applications")]):
        if shutil.which(command[0]):
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def update(here, check_only=False):
    here = Path(here).resolve()
    try:
        info = json.loads((here / "install.json").read_text())
        prefix = Path(info["prefix"])
        repository, branch = info["repository"], info["branch"]
        current = info["revision"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Run ./install.sh once to register this installation before using 'earbridge update'.") from None
    if not prefix.is_absolute() or (prefix / "share/earbridge").resolve() != here:
        raise RuntimeError("The installation location does not match its update information; run ./install.sh again.")
    print("Checking for Earbridge updates…", file=sys.stderr, flush=True)
    remote = git("ls-remote", "--exit-code", "--heads", "--", repository, f"refs/heads/{branch}").split()
    if not remote:
        raise RuntimeError(f"No update branch named {branch} was found.")
    latest = remote[0]
    result = {"current": current, "latest": latest, "update_available": current != latest, "updated": False, "warnings": []}
    if check_only or current == latest:
        return result
    if not os.access(here.parent, os.W_OK):
        raise RuntimeError(f"The installation at {prefix} is not writable. Run the update as its owner.")
    with (here.parent / ".earbridge-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Earbridge update is already running.") from None
        with ExitStack() as stack:
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="earbridge-update-")))
            source = temporary / "source"
            print("Downloading and preparing the update…", file=sys.stderr, flush=True)
            git("clone", "--quiet", "--depth", "1", "--single-branch", "--branch", branch, "--", repository, str(source))
            result["latest"] = git("rev-parse", "HEAD", cwd=source)
            if result["latest"] == current:
                result["update_available"] = False
                return result
            stage = temporary / "stage"
            env = dict(os.environ, PREFIX=str(prefix), DESTDIR=str(stage))
            try:
                subprocess.run(["sh", str(source / "install.sh")], env=env, check=True,
                               capture_output=True, text=True, timeout=120)
            except subprocess.CalledProcessError as error:
                raise RuntimeError(f"The update could not be prepared; installation unchanged: {error.stderr.strip()}") from error
            staged_prefix = stage / prefix.relative_to(prefix.anchor)
            replacement = PreparedInstall(staged_prefix, prefix, stack)
            runtime = Runtime()
            print("Installing the update…", file=sys.stderr, flush=True)
            try:
                runtime.stop()
                replacement.apply()
                result["warnings"] = runtime.resume()
            except BaseException as error:
                recovery_errors = []
                if any(e["installed"] for e in replacement.entries):
                    try:
                        runtime.stop_service()
                    except Exception as problem:
                        recovery_errors.append(str(problem))
                replacement.rollback()
                if runtime.stopped:
                    try:
                        runtime.resume()
                    except Exception as problem:
                        recovery_errors.append(str(problem))
                message = f"Update failed; restored the previous installation: {error}"
                if recovery_errors:
                    message += "; restart needs attention: " + "; ".join(recovery_errors)
                raise RuntimeError(message) from error
            result["updated"] = True
            result["update_available"] = False
    try:
        refresh_desktop(prefix)
    except (OSError, subprocess.TimeoutExpired) as error:
        result["warnings"].append(f"Desktop cache refresh: {error}")
    return result
