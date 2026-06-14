# Wraith 🖤 — ghost in your phone

A lightweight QtScrcpy alternative: **mirror your Android screen + audio** and
play mobile FPS games with a **keyboard + mouse**, using a **drag-and-drop visual
keymap editor**.

Wraith is clever about it — it does **not** re-decode video. Real `scrcpy` shows
the mirror window (hardware-accelerated, audio built in). Wraith adds a separate,
low-latency **control layer** that speaks scrcpy's binary protocol directly to
inject multi-touch events — fast enough for aiming.

```
┌─────────────┐     video+audio      ┌──────────────┐
│   scrcpy    │◀─────────────────────│              │
│ (mirror win)│                      │   ANDROID    │
└─────────────┘                      │   (phone)    │
┌─────────────┐  touch inject (TCP)  │              │
│   Wraith    │─────────────────────▶│              │
│ kbd+mouse → │   own control server └──────────────┘
│   touches   │
└─────────────┘
```

## Features
- 🪞 Screen mirror + audio (via `scrcpy` 2.x/4.x)
- 🎯 **Mouse-look aim** — relative mouse → camera drag with auto-recenter (infinite turn)
- 🕹️ **WASD movement joystick**
- 🔫 Tap binds for fire/ADS/jump/crouch/reload/use (mouse buttons supported)
- 🎨 **Drag-and-drop editor** — drop bubbles, drag to position, double-click to rebind
- 🪶 No heavy deps: stdlib `tkinter` + `pynput`. Runs on Python 3.14.
- ⚙️ Mirror tuned for thermally-limited phones (H.264, 30 fps, capped size)

## Requirements (cross-platform: macOS / Windows / Linux)
- `scrcpy` + `adb` on PATH
- Python 3.10+ (with tkinter)
- `pynput` (the launcher installs it for you)

### macOS
- `brew install scrcpy android-platform-tools`
- If using Homebrew Python: `brew install python-tk@<ver>` for tkinter
- **One-time permissions**: System Settings → Privacy & Security → grant your
  terminal app **Accessibility** *and* **Input Monitoring**.

### Windows 10/11
- Install scrcpy: `scoop install scrcpy` or `choco install scrcpy`, **or**
  download the zip from the scrcpy releases and add its folder to PATH
  (it contains `scrcpy.exe`, `adb.exe`, and `scrcpy-server` together).
- Install Python from python.org (includes tkinter — no extra step).
- **No special permissions needed** on Windows.
- If Wraith can't find the server file, set it explicitly:
  `set SCRCPY_SERVER_PATH=C:\path\to\scrcpy\scrcpy-server`

## Install / Run
**macOS / Linux:**
```bash
cd ~/wraith
./wraith.sh --help        # first run builds a venv + installs pynput
```
**Windows:**
```bat
cd wraith
wraith.bat --help         REM first run builds a venv + installs pynput
```
(Replace `./wraith.sh` with `wraith.bat` in all examples below.)

## Usage — GUI (easiest, no shortcuts needed)
Just launch with no arguments and click buttons:
```bash
./wraith.sh            # macOS/Linux   (Windows: wraith.bat)
```
The launcher lets you pick a device + keymap, **Edit** keymaps (drag-and-drop),
set mirror options, and ▶ **Play** / ■ **Stop** / toggle **Capture** — all by
button. (F8/F9 hotkeys still work if you prefer them.)

## Usage — command line
Design / edit a keymap (no phone needed):
```bash
./wraith.sh --edit
```
Play with the bundled Delta Force map (phone plugged in, USB debugging on):
```bash
./wraith.sh -k keymaps/delta_force.json
```
Edit the Delta Force map, then play:
```bash
./wraith.sh -k keymaps/delta_force.json --edit
```

### In-game keys
| Key | Action |
|-----|--------|
| **CTRL** (switch key) | Toggle GAME mode (aim/keymap) vs MOUSE mode (free cursor) |
| **F10** | In-window keymap editor — drag keys onto the live video, save, keep playing |
| **F12** | Record video+audio to ~/Videos/Wraith |
| **F9** | Quit Wraith |
| Mouse move | Aim / camera look |
| WASD | Move |
| your bound keys | Taps (fire, jump, reload, …) |

### Useful flags
```
-s SERIAL        target a specific adb device
--no-mirror      skip the scrcpy window (keymap injection only)
--fps 30         mirror frame cap (default 30 — matches CRYO Marathon)
--max-size 1280  mirror resolution cap
--bitrate 6M     mirror bitrate
--toggle f8 --quit-key f9
-v               verbose logging
```

## Tuning notes (read me!)
- **Aim sensitivity** lives in the keymap (`sensitivity`, also editable by
  double-clicking the AIM bubble). Start at `1.0`, raise for faster turns.
- The bundled Delta Force positions are **starting guesses** — open `--edit`
  with the phone mirrored, and drag each bubble to sit exactly on the game's
  on-screen buttons.
- **Coordinate space / rotation**: positions are normalized over the *landscape*
  screen. If touches land rotated or mirrored on first run, that's the one thing
  that needs a quick live calibration pass with the phone connected (orientation
  handling differs per device/game). Ping me and we'll lock it in.
- Mouse-look uses the *flick-recenter* technique: when the virtual finger leaves
  the look box it lifts and re-anchors, giving you unlimited turn distance.

## Architecture
| File | Role |
|------|------|
| `wraith/control.py` | scrcpy control-socket protocol + control-only server bootstrap |
| `wraith/keymap.py`  | keymap data model + JSON load/save |
| `wraith/injector.py`| keyboard/mouse → multi-touch engine (joystick, taps, mouse-look) |
| `wraith/capture.py` | global input capture (pynput) + relative-mouse warp |
| `wraith/editor.py`  | tkinter drag-and-drop keymap editor |
| `wraith/app.py`     | orchestrator / CLI |

## Status
v0.1 — code-complete. Mirror + editor + injection protocol all implemented.
End-to-end touch injection wants one short calibration pass on a connected
device (sensitivity + orientation).
