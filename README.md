# Wraith 🖤 — ghost in your phone

A lightweight QtScrcpy alternative: **mirror your Android screen + audio** and
play mobile FPS games with a **keyboard + mouse**, using an **in-window
drag-and-drop keymap editor**.

Wraith is an **integrated client** — video, input, audio and recording all live
in **one window**. It talks `scrcpy`'s binary protocol directly: it bootstraps
the bundled `scrcpy-server` over adb, pulls the encoded H.265/H.264 stream, and
**decodes it itself** (PyAV/ffmpeg, hardware-accelerated where available). The
same window reads your keyboard + mouse **in-window via SDL** — no global system
hooks — and injects multi-touch events back to the phone, fast enough for aiming.

```
        ┌──────────────────────────────┐
        │            WRAITH            │   one integrated window
        │  ┌────────────────────────┐  │
        │  │  decode + display      │◀─┼─── video + audio  ┌──────────────┐
        │  │  (PyAV / SDL / audio)  │  │                   │              │
        │  ├────────────────────────┤  │                   │   ANDROID    │
        │  │  kbd + mouse (in-win)  │  │                   │   (phone)    │
        │  │      → multi-touch     │──┼─── touch inject ──▶│              │
        │  └────────────────────────┘  │   (scrcpy proto)  └──────────────┘
        └──────────────────────────────┘
```

## Features
- 🪞 Screen mirror + audio, decoded in-window (H.265/HEVC by default, H.264 fallback)
- 🎯 **Mouse-look aim** — the look finger drags across the whole screen and
  re-anchors only rarely, inside a button-free clean zone → smooth, flick-free turns
- 🕹️ **WASD movement joystick**
- 🔫 Tap binds for fire/ADS/jump/crouch/reload/use (mouse buttons supported)
- 🎨 **In-window F10 editor** — drag bubbles onto the *live* video, save, keep playing
- 🎥 **F12 recording** to `~/Videos/Wraith` — no re-encode (≈free), and your PC
  mic is teed in so in-game **voice chat keeps working while you record**
- 🪶 No global input hooks: input is read in-window via `pygame`/SDL
- ⚙️ Tuned defaults for thermally-limited phones (H.265 + CBR, 60 fps, max-size 1920)
- 📦 Ships as a **self-contained signed Windows app** — bundles adb, scrcpy-server
  and ffmpeg; nothing to install

## Install / Run
### Windows (recommended) — the installer
Grab the latest **Wraith setup** / `Wraith-windows.zip`, install (or unzip), and
launch **`Wraith.exe`** (or double-click **`Start-Wraith.bat`**). It's a
self-contained PyInstaller *onedir* build with **adb, `scrcpy-server` and ffmpeg
bundled** — no Python, no scrcpy on PATH, no permissions to grant. Built-in
auto-updater checks for new versions on launch.

### From source (dev)
```powershell
# first run builds a venv + installs deps, and pulls scrcpy/ffmpeg via winget if missing
.\Start-Wraith.bat
# or, directly:
python -m wraith
```
Source deps (see `requirements.txt`): `av`, `pygame-ce`, `numpy`,
`opencv-python-headless`, `sounddevice`. `tkinter` ships with Python. `scrcpy`/`adb`
are used from the bundle when frozen, else from PATH; `ffmpeg` is only needed for
recording.

## Usage — the launcher (QtScrcpy-style control panel)
Launching opens a control panel:
- **left** — device list (double-click to connect) + a live adb console
- **right** — Start Config (bitrate / max size / fps / codec / record path /
  options), a **performance preset** (Auto detects this PC's hw decode + cores,
  or pick Low / Medium / High), plus USB and Wireless (adb-over-TCP) lines

Pick a device + keymap, set options, hit **▶ Start** — the mirror window opens.
Edit keymaps live with **F10** once you're in.

## In-game keys
| Key | Action |
|-----|--------|
| **CTRL** (switch key) | Toggle GAME mode (aim/keymap) ⇄ MOUSE mode (free cursor — click to tap, **type to chat**) |
| **F10** | In-window keymap editor — drag keys onto the live video, save, keep playing |
| **F12** | Start/stop recording (video + audio + your PC mic) → `~/Videos/Wraith` |
| **F9** | Quit Wraith |
| Mouse move | Aim / camera look (GAME mode) |
| WASD | Move |
| your bound keys | Taps (fire, jump, reload, …) |

## Tuning notes (read me!)
- **Set your monitor to 60 Hz** for smooth 60 fps mirroring. Odd refresh rates
  (e.g. 99 Hz) don't divide evenly into 60 and will **judder**.
- **Aim feel**: the look finger drags across nearly the whole screen and only
  re-anchors when it runs out of room — and when it does, it re-presses inside a
  button-free **clean zone** (central-right of the landscape screen), so a
  re-anchor never lands on a control or flicks your view (`injector.py
  _aim_tick`). Normal looking and recoil never reach an edge, so they never flick.
- **Aim sensitivity** lives in the keymap (`sensitivity`, also editable by
  double-clicking the AIM bubble). Start at `1.0`, raise for faster turns.
- **Max size**: `1920` is the sharpest a 1080p display can show; `0 (native)`
  adds latency with no visible gain.
- The bundled positions are **starting guesses** — open the **F10** editor with
  the phone mirrored and drag each bubble onto the game's on-screen buttons.

## Architecture
| File | Role |
|------|------|
| `wraith/launcher.py` | QtScrcpy-style control panel (tkinter); spawns the mirror window |
| `wraith/mirror.py`   | the integrated client window: decode + display + audio + recording + input loop |
| `wraith/control.py`  | scrcpy control-socket protocol + server bootstrap |
| `wraith/injector.py` | keyboard/mouse → multi-touch engine (joystick, taps, mouse-look) |
| `wraith/keymap.py`   | keymap data model + JSON load/save |
| `wraith/editor.py`   | in-window (F10) drag-and-drop keymap editor |
| `wraith/perf.py`     | performance presets + this-PC auto-detection |
| `wraith/runtime.py`  | resolves bundled binaries / writable paths (frozen vs dev) |
| `wraith/toolbar.py`  | in-window MOUSE-mode toolbar |
| `wraith/updater.py`  | auto-update check + installer download |

## Status
**v0.4.2** — shipping. Integrated client (video + input + audio + recording in one
window), in-window F10 editor, flick-free full-screen aim, and a self-contained
signed Windows installer with bundled adb / scrcpy-server / ffmpeg.
