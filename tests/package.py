"""Run on a private bus: dbus-run-session -- python3 tests/package.py."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from gi.repository import Gio

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='earbridge package ') as temporary:
    prefix = Path(temporary)
    env = dict(os.environ, PREFIX=temporary, XDG_CONFIG_HOME=str(prefix / 'config'), XDG_STATE_HOME=str(prefix / 'state'))
    subprocess.run(['sh', str(ROOT / 'install.sh')], env=env, check=True)
    desktop = prefix / 'share/applications/earbridge.desktop'
    subprocess.run(['desktop-file-validate', str(desktop)], check=True)
    binary = prefix / 'bin/earbridge'
    subprocess.run([str(binary), '--help'], check=True, stdout=subprocess.DEVNULL)
    app = Gio.DesktopAppInfo.new_from_filename(str(desktop))
    assert app.get_commandline().endswith(' ui'), app.get_commandline()
    # Record the launcher's actual argv, including correct parsing of spaced paths.
    recorder = prefix / 'arguments.json'
    binary.unlink()
    binary.write_text(f'#!/usr/bin/python3\nimport json, sys\nfrom pathlib import Path\nPath({str(recorder)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    binary.chmod(0o755)
    assert app.launch([], None)
    deadline = time.monotonic() + 3
    while not recorder.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(recorder.read_text()) == ['ui']
    binary.unlink()
    binary.symlink_to(prefix / 'share/earbridge/earbridge.py')
    subprocess.run(['sh', str(ROOT / 'install.sh'), '--uninstall'], env=env, check=True)
    assert not desktop.exists()
    assert not binary.exists()
print('PASS: installation, CLI, app launcher invokes UI, uninstall (prefix with spaces)')
