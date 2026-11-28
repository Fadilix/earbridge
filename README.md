<div align="center">

<img src="earbridge.svg?v=3" alt="Earbridge logo" width="112">

# Earbridge

**One pair of headphones. Every device.**

Play your phone, tablet or laptop through the headphones on your PC. Wireless from each device to the PC, one cable (or one Bluetooth link) from the PC to your ears.

![Linux](https://img.shields.io/badge/Linux-only-1e1b4b?logo=linux&logoColor=white)
![PipeWire](https://img.shields.io/badge/audio-PipeWire-6366f1)
![BlueZ](https://img.shields.io/badge/bluetooth-BlueZ-6366f1)
![GTK4](https://img.shields.io/badge/UI-GTK4%20%2B%20libadwaita-5eead4?labelColor=1e1b4b)

</div>

---

## The problem

Most phones no longer have a 3.5 mm headphone jack. If you own good wired headphones, they're stuck on your computer, while the audio you want to hear (a call, a video, music, a game) is on your phone.

The usual fixes aren't great:

- A USB-C to 3.5 mm dongle is one more thing to carry and lose, and you can't charge the phone while it's plugged in.
- New Bluetooth headphones cost money, and you already own headphones you like.
- Most streaming apps need an app on both devices, an account, and often a subscription.

## The solution

Your Linux PC can already act as a **Bluetooth speaker**. BlueZ and PipeWire can receive audio from a phone over Bluetooth (A2DP) and play it on any output, including the jack your headphones are plugged into. It's just hidden behind `bluetoothctl` commands and adapter settings you'd have to undo by hand afterwards.

Earbridge puts that behind one switch:

```mermaid
flowchart LR
    P["📱 Phone"] -- "Bluetooth audio (A2DP)" --> B["Earbridge<br/>(BlueZ + PipeWire)"]
    B -- "chosen output" --> H["🎧 Wired headphones"]
```

| Reception **on** | Reception **off** |
| --- | --- |
| The PC becomes visible and pairable as a Bluetooth speaker | Phones Earbridge connected are disconnected |
| Pairing requests appear in notifications and the optional window | Visibility, pairing and power go back to what they were before |
| The phone's audio goes to the output you pick | Nothing on the phone or PC stays changed |

No app on the phone: it pairs with your PC like it would with any Bluetooth speaker.

Earbridge runs as a background service. The CLI, window, and tray all control the same service. **Closing or killing the window keeps phone audio playing.** Use `earbridge stop` to disable reception, or `earbridge quit` to stop reception and exit the service and tray.

## Features

- **CLI controls** for reception, phone connections, outputs, and pairing approvals.
- **A window that shows the whole route**: which device you're listening to, this PC, and the headphones it plays on, with a live indicator while audio flows. It also shows battery level, what's playing (with play/pause/next/previous), and one button to start listening.
- **Tray indicator** with reception status, Open Earbridge, Enable/Disable reception, and Quit Earbridge. Clicking the icon opens the window.
- **Pick the output** ("Play through"): headphones, HDMI, or the system default. Only the phone's audio is moved; your other PC sounds stay where they are.
- **Pick the devices**: paired phones, tablets and laptops are listed with a connect switch each.
- **Add devices from the PC**: search for nearby devices and pair them from the window or with `earbridge scan on` and `earbridge pair`. You still approve the code.
- **You approve every new phone**: compare the 6-digit code and approve in the notification, window, or CLI. Requests expire after 60 seconds and are never accepted automatically.
- **Remembers the chosen output** across service restarts.
- **Restores your Bluetooth settings** when reception stops. If the service crashes or is killed, recovery runs when it next starts.
- **Optional reception at login** with `earbridge autostart on`.
- **Update in place** with `earbridge update`, including restarting the running service and reconnecting phones.

## Requirements

- Linux with **PipeWire** (with Bluetooth support) and **BlueZ**
- A Bluetooth adapter
- Python 3 with **PyGObject**, **GTK 4** and **libadwaita**
- **Git** to download updates
- A desktop session D-Bus. A **StatusNotifierItem** tray host is needed to show the panel icon (for example, a Waybar `tray` module). Without a tray host, the CLI and window still work. Notification buttons require a notification server with action support; the window and CLI can always approve requests.

On Arch Linux:

```sh
sudo pacman -S --needed python-gobject gtk4 libadwaita pipewire-audio libpulse bluez bluez-utils util-linux git
sudo systemctl enable --now bluetooth
```

The installer checks these and tells you what's missing.

## Install

```sh
git clone git@github.com:Fadilix/earbridge.git
cd earbridge
./install.sh                          # for your user, in ~/.local (no sudo)
sudo PREFIX=/usr/local ./install.sh   # or for every user on the PC
```

This installs the `earbridge` CLI, an app launcher entry, and the icon. **Earbridge in the app list opens the UI** (`earbridge ui`).

## Update

```sh
earbridge update --check           # Check for a newer commit without changing anything
earbridge update                   # Download and install the latest main
earbridge --json update --check    # Machine-readable update information
```

Updates use the repository recorded at installation, normally `https://github.com/Fadilix/earbridge.git`, and install into the same prefix. They do not change your source checkout, output preference, or login startup setting. GitHub's official SSH clone URL is converted to HTTPS for updates, so the installed command does not need an SSH key.

The updater prepares the new files before stopping anything. If Earbridge is running, it restarts the service, restores reception to its previous state, attempts to reconnect connected phones, and reopens the window if it was open. Audio may briefly disconnect. Complete pending pairing requests before updating. If installation or service startup fails, it restores the previous files and attempts to resume the previous session. An already current installation is left running.

Run updates as the installation's owner; a system-wide installation needs write access to its prefix. Only the invoking desktop session is restarted. Other logged-in users should restart Earbridge to load the new version.

For a version installed before `earbridge update` was added, first run `git pull` and `./install.sh` from the source checkout once. Running `./install.sh` again is also available for installing local changes.

To uninstall (use the same `PREFIX` you installed with):

```sh
./install.sh --uninstall
```

## Usage

Run `earbridge` without arguments for CLI help. To receive audio without a window:

```sh
earbridge start                    # Enable reception; returns to the terminal
earbridge status                   # Reception, output, connected phones, errors
earbridge outputs                  # Available output names and descriptions
earbridge output <output-name>     # Route phone audio to this output
earbridge output default           # Use the system default for incoming streams
```

On the phone, open Bluetooth settings and pick the PC. Compare the pairing code, then approve the request in the notification or with the CLI:

```sh
earbridge requests                 # Show the question, code and request ID
earbridge approve <request-id>     # Approve that specific pending request
earbridge deny <request-id>        # Reject it instead
```

Play something on the phone. Next time, it is already paired: enable reception and connect from the phone or the CLI. Phone selectors accept a Bluetooth address or an exact, unambiguous name; quote names with spaces.

```sh
earbridge phones
earbridge connect AA:BB:CC:DD:EE:FF
earbridge scan on                  # Look for nearby devices for 45 seconds
earbridge nearby                   # List what was found
earbridge pair AA:BB:CC:DD:EE:FF   # Pair and connect it (approve the code when asked)
earbridge media pause              # play, pause, next or previous on the connected device
earbridge disconnect AA:BB:CC:DD:EE:FF
earbridge ui                       # Open the window; reception stays as it is
earbridge stop                     # Disable reception and restore Bluetooth
earbridge quit                     # Also exit the background service and tray
earbridge --json status            # JSON output for scripts
```

Alternatively, open **Earbridge** from your app launcher, turn on **Receive phone audio**, and select your headphones in **Play through**. The window can be closed at any time without interrupting audio. The tray stays available while the service runs, even with reception disabled.

Reception at login is off by default:

```sh
earbridge autostart on
earbridge autostart status
earbridge autostart off
```

These commands manage `~/.config/autostart/earbridge.desktop` (or the equivalent under `XDG_CONFIG_HOME`). Disabling login startup does not stop the current session.

## How it works

| File | Role |
| --- | --- |
| `earbridge.py` | CLI and explicit `ui` entry point. |
| `service.py` | One background service per desktop session, with D-Bus commands and notifications. |
| `bridge.py` | Bridge lifetime, saved adapter state, shutdown and crash recovery. |
| `ipc.py` | Shared session-bus client and detached process startup. |
| `ui.py` | GTK4/libadwaita window; a client of the service. |
| `tray.py` | Independent StatusNotifierItem and D-Bus menu; no AppIndicator/GTK3 dependency. |
| `updater.py` | Git updates, staged installation, process restart and rollback. |
| `bluez.py` | Talks to BlueZ over D-Bus: adapter settings, the phones list, and a pairing agent that asks you before accepting a phone. |
| `audio.py` | Watches PipeWire (through `pactl`) and moves incoming Bluetooth audio to the chosen output. |

While reception is on, the adapter's previous state is written to `~/.local/state/earbridge/session.json`. If the service doesn't shut down cleanly, its next launch restores those settings before a new session starts. Normal shutdown waits for new phone disconnections before restoring adapter power; previously connected phones are preserved. `SIGINT`, `SIGTERM`, and `SIGHUP` request graceful service shutdown. A failed restoration retains the journal; retry with `earbridge stop`.

The selected output is saved in `~/.config/earbridge/settings.json`. Background logs are in `~/.local/state/earbridge/_service.log` and `_tray.log`; UI launches from the tray also log to `ui.log`. State and config paths respect `XDG_STATE_HOME` and `XDG_CONFIG_HOME`.

## Development checks

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
dbus-run-session -- python3 tests/integration.py
dbus-run-session -- python3 tests/package.py
dbus-run-session -- python3 tests/update_integration.py
```

The integration check uses fake Bluetooth/audio hardware on an isolated D-Bus session and exercises the actual CLI, service, tray menu and notifications. If `gtk4-broadwayd` is installed, it also checks that closing or killing the real GTK window leaves reception running. No phone or live desktop session is needed.

Updater checks use a local Git remote and temporary installations, including a running service/window update and recovery from a failed service startup. They do not download from GitHub or touch your installed copy.

## FAQ

**Is there a delay?**
Bluetooth audio has about 100–200 ms of latency. That's fine for music and video, but you may notice it in fast games.

**Does it work on macOS or Windows?**
No. Earbridge relies on BlueZ and PipeWire, which only exist on Linux, and macOS can't receive Bluetooth audio from a phone at all. On a Mac, an iPhone can use AirPlay (built in since macOS Monterey); for Android, use a Wi-Fi app such as AudioRelay or `scrcpy --no-video`.

**My phone asks me to type a PIN.**
Only very old phones use PIN pairing, and Earbridge doesn't support it. Modern Android phones and iPhones show a code to compare instead.

**Nothing plays after pairing.**
On the phone, make sure the PC is selected as the media output, and that "Media audio" is enabled for it in the Bluetooth device settings.
