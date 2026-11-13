<div align="center">

<img src="earbridge.svg?v=2" alt="Earbridge logo" width="112">

# Earbridge

**Listen to your phone through the headphones plugged into your PC.**

Wireless from the phone to the PC, wired from the PC to your ears.

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

| Switch **on** | Switch **off** (or close the window) |
| --- | --- |
| The PC becomes visible and pairable as a Bluetooth speaker | Phones Earbridge connected are disconnected |
| Pairing requests appear as a popup with the code to compare | Visibility, pairing and power go back to what they were before |
| The phone's audio goes to the output you pick | Nothing on the phone or PC stays changed |

No app on the phone: it pairs with your PC like it would with any Bluetooth speaker.

## Features

- **One switch** to turn the bridge on and off.
- **Pick the output** ("Play through"): headphones, HDMI, or the system default. Only the phone's audio is moved; your other PC sounds stay where they are.
- **Pick the phones**: paired phones are listed with a connect switch each.
- **You approve every new phone**: pairing shows the 6-digit code to compare, and a phone needs your OK before it can play audio.
- **Restores your settings exactly**, even if the app crashes or is killed while on (they're restored on the next launch).

## Requirements

- Linux with **PipeWire** (with Bluetooth support) and **BlueZ**
- A Bluetooth adapter
- Python 3 with **PyGObject**, **GTK 4** and **libadwaita**

On Arch Linux:

```sh
sudo pacman -S --needed python-gobject gtk4 libadwaita pipewire-audio bluez bluez-utils
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

This installs the `earbridge` command, an app launcher entry, and the icon.

To uninstall (use the same `PREFIX` you installed with):

```sh
./install.sh --uninstall
```

## Usage

1. Run `earbridge` (or open **Earbridge** from your app launcher).
2. Turn on **Receive phone audio**. The PC shows up on your phone under its Bluetooth name.
3. On the phone, open Bluetooth settings and pick the PC. Accept the popups on the PC: check that both screens show the same code.
4. Choose your headphones in **Play through** and play something on the phone.
5. Turn the switch off when you're done.

Next time, the phone is already paired: turn the switch on and connect it from the phone, or with its switch in the **Phones** list.

## How it works

| File | Role |
| --- | --- |
| `earbridge.py` | The window. Saves the adapter state when turned on and restores it when turned off. |
| `bluez.py` | Talks to BlueZ over D-Bus: adapter settings, the phones list, and a pairing agent that asks you before accepting a phone. |
| `audio.py` | Watches PipeWire (through `pactl`) and moves incoming Bluetooth audio to the chosen output. |

While the switch is on, the adapter's previous state is also written to `~/.local/state/earbridge/session.json`. If Earbridge doesn't shut down cleanly, the next launch reads that file and restores your settings.

## FAQ

**Is there a delay?**
Bluetooth audio has about 100–200 ms of latency. That's fine for music and video, but you may notice it in fast games.

**Does it work on macOS or Windows?**
No. Earbridge relies on BlueZ and PipeWire, which only exist on Linux, and macOS can't receive Bluetooth audio from a phone at all. On a Mac, an iPhone can use AirPlay (built in since macOS Monterey); for Android, use a Wi-Fi app such as AudioRelay or `scrcpy --no-video`.

**My phone asks me to type a PIN.**
Only very old phones use PIN pairing, and Earbridge doesn't support it. Modern Android phones and iPhones show a code to compare instead.

**Nothing plays after pairing.**
On the phone, make sure the PC is selected as the media output, and that "Media audio" is enabled for it in the Bluetooth device settings.
