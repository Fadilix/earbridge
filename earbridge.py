#!/usr/bin/env python3
"""Earbridge CLI. GTK is loaded only by the explicit 'ui' command."""

import argparse
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def desktop_exec(binary):
    """Quote an executable for both desktop-entry value and Exec parsing."""
    value = str(binary).replace("\\", "\\\\\\\\")
    for character in ('"', '`', '$'):
        value = value.replace(character, "\\\\" + character)
    return '"' + value.replace("%", "%%") + '"'


def parser():
    cli = argparse.ArgumentParser(description="Receive phone audio in the background over Bluetooth.")
    cli.add_argument("--json", action="store_true", help="print machine-readable JSON")
    commands = cli.add_subparsers(dest="command")
    for name, help_text in (
        ("start", "enable reception in the background"),
        ("stop", "disable reception and restore Bluetooth settings"),
        ("status", "show service and bridge state"),
        ("phones", "list paired phones"),
        ("outputs", "list audio outputs"),
        ("requests", "list pending pairing and audio authorization requests"),
        ("ui", "open the graphical interface"),
        ("quit", "stop reception and exit the background service and tray"),
    ):
        commands.add_parser(name, help=help_text)
    for name in ("connect", "disconnect"):
        sub = commands.add_parser(name, help=f"{name} a phone by address or exact name")
        sub.add_argument("phone")
    sub = commands.add_parser("output", help="select an output by name; use 'default' for the system default")
    sub.add_argument("output")
    for name in ("approve", "deny"):
        sub = commands.add_parser(name, help=f"{name} a pending request after reviewing it")
        sub.add_argument("request_id", help="ID from 'earbridge requests'")
    sub = commands.add_parser("autostart", help="enable or disable background reception at login")
    sub.add_argument("setting", choices=("on", "off", "status"))
    sub = commands.add_parser("update", help="update this installation from main")
    sub.add_argument("--check", action="store_true", help="check for updates without installing them")
    for name in ("_service", "_tray"):
        commands.add_parser(name, help=argparse.SUPPRESS)
    # Hide internal entry points from the help command list.
    commands._choices_actions = [a for a in commands._choices_actions if not a.dest.startswith("_")]
    commands.metavar = "{" + ",".join(n for n in commands.choices if not n.startswith("_")) + "}"
    return cli


def autostart(setting):
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    entry = config / "autostart/earbridge.desktop"
    if setting == "on":
        entry.parent.mkdir(parents=True, exist_ok=True)
        binary = desktop_exec(HERE / "earbridge.py")
        entry.write_text('[Desktop Entry]\nType=Application\nName=Earbridge\n'
                         f'Exec={binary} start\nIcon=earbridge\nTerminal=false\n'
                         'Comment=Receive phone audio in the background\n')
    elif setting == "off":
        entry.unlink(missing_ok=True)
    return {"autostart": entry.exists()}


def display(command, result):
    if command == "status":
        print(f"Service: {'running' if result['running'] else 'stopped'}")
        print(f"Reception: {'on' if result['active'] else 'off'}")
        if result.get("busy"):
            print("Bluetooth is changing state")
        if result.get("recovery_pending"):
            print("Bluetooth restoration is pending; run 'earbridge stop' to retry")
        if result.get("adapter"):
            print(f"Bluetooth name: {result['adapter']}")
        print(f"Output: {result.get('output') or 'System default'}")
        for phone in result.get("phones", []):
            if phone["connected"]:
                print(f"Connected: {phone['name']} ({phone['address']})")
        if result.get("requests"):
            print("Pairing/authorization pending: run 'earbridge requests'")
        if result.get("error"):
            print(f"Error: {result['error']}")
    elif command in ("phones", "outputs", "requests"):
        if not result:
            print(f"No {command}.")
        for item in result:
            if command == "phones":
                print(f"{item['address']}  {item['name']}  {'connected' if item['connected'] else 'disconnected'}")
            elif command == "outputs":
                print(f"{item['name']}  {item['description']}")
            else:
                print(f"{item['id']}  {item['question']}\n  earbridge approve {item['id']} | earbridge deny {item['id']}")
    elif command == "autostart":
        print(f"Start at login: {'on' if result['autostart'] else 'off'}")
    elif command == "update":
        if result["updated"]:
            print(f"Updated Earbridge to {result['latest'][:7]}.")
        elif result["update_available"]:
            print(f"Update available: {result['latest'][:7]}. Run 'earbridge update' to install it.")
        else:
            print(f"Earbridge is up to date ({result['latest'][:7]}).")
        for warning in result["warnings"]:
            print(warning, file=sys.stderr)
    else:
        print(result or "Done.")


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if not args.command:
        cli.print_help()
        return 0
    try:
        if args.command == "autostart":
            result = autostart(args.setting)
        elif args.command == "update":
            from updater import update
            result = update(HERE, check_only=args.check)
        elif args.command == "_service":
            from service import run
            return run()
        elif args.command == "_tray":
            from tray import run
            return run()
        elif args.command == "ui":
            from ui import run
            return run()
        else:
            import ipc
            no_service = args.command in ("status", "stop", "quit") and not ipc.running()
            needs_recovery = (ipc.STATE_DIR / "session.json").exists()
            if no_service and (args.command == "status" or not needs_recovery):
                result = {"running": False, "active": False} if args.command == "status" else "Earbridge is stopped."
                if args.command == "status":
                    result["recovery_pending"] = needs_recovery
            else:
                connection = ipc.ensure_service()
                arguments = {k: v for k, v in vars(args).items() if k not in ("command", "json")}
                result = ipc.command(args.command, connection=connection, **arguments)
        if args.json:
            print(json.dumps(result))
        else:
            display(args.command, result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"earbridge: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
