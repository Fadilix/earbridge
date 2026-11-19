"""Bridge lifetime and recovery, independent of any window or tray."""

import json

from ipc import write_json


class Bridge:
    def __init__(self, bt, router, session_file, ask, changed):
        self.bt = bt
        self.router = router
        self.session_file = session_file
        self.ask = ask
        self.changed = changed
        self.session = None
        self.active = False
        self.busy = False
        self.allowed = set()
        if session_file.exists():
            # A damaged journal must never be silently overwritten.
            self.session = json.loads(session_file.read_text())
            if not isinstance(self.session, dict) or not isinstance(self.session.get("adapter"), dict) or not isinstance(self.session.get("connected"), list):
                raise RuntimeError(f"Invalid recovery journal: {session_file}")

    def start(self, done):
        if self.busy:
            return done("Bluetooth is changing state; try again shortly")
        if self.active:
            return done(None)
        if self.session:
            return self.stop(lambda error: done(error) if error else self.start(done))
        self.busy = True
        try:
            self.session = {
                "adapter": self.bt.snapshot(),
                "adapter_path": self.bt.adapter,
                "connected": [p["path"] for p in self.bt.phones() if p["connected"]],
            }
            write_json(self.session_file, self.session)
            self.bt.set_adapter("Powered", True)
            self.bt.set_adapter("Pairable", True)
            self.bt.set_adapter("DiscoverableTimeout", 0)
            self.bt.set_adapter("Discoverable", True)
            self.bt.register_agent(self.ask, self.allowed)
            self.router.start()
            self.active = True
        except Exception as error:
            self.busy = False
            message = str(error)
            return self.stop(lambda cleanup: done(message + (f"; cleanup: {cleanup}" if cleanup else "")))
        self.busy = False
        self.changed()
        done(None)

    def stop(self, done):
        if self.busy:
            return done("Bluetooth is changing state; try again shortly")
        if not self.session:
            return done(None)
        self.busy = True
        try:
            if self.session.get("adapter_path", self.bt.adapter) != self.bt.adapter:
                raise RuntimeError("The saved session belongs to another Bluetooth adapter")
            self.active = False
            self.router.stop()
            self.ask(None, None, None)
            self.bt.unregister_agent()
            before = set(self.session["connected"])
            pending = [p["path"] for p in self.bt.phones() if p["connected"] and p["path"] not in before]
        except Exception as error:
            self.busy = False
            self.changed()
            return done(str(error))

        errors = []
        remaining = len(pending)

        def restore():
            try:
                self.bt.restore(self.session["adapter"])
                if not errors:
                    self.session_file.unlink(missing_ok=True)
                    self.session = None
                    self.allowed.clear()
            except Exception as error:
                errors.append(str(error))
            self.busy = False
            self.changed()
            done("; ".join(errors) if errors else None)

        def disconnected(error):
            nonlocal remaining
            if error:
                errors.append(error)
            remaining -= 1
            if remaining == 0:
                restore()

        # Wait for disconnect replies before powering the adapter down.
        if pending:
            for path in pending:
                try:
                    self.bt.disconnect(path, disconnected)
                except Exception as error:
                    disconnected(str(error))
        else:
            restore()
