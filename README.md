# Wraith 🖤 — ghost in your phone

Mirror your Android screen + audio to Windows and play mobile FPS games with a
**keyboard + mouse**, using an in-window **drag-and-drop keymap editor**.
Low-latency, scrcpy-powered, and fully self-contained.

Wraith is a native Windows client built on a customized **scrcpy / QtScrcpy**
engine. It bootstraps the bundled `scrcpy-server` over adb, pulls the encoded
**H.265/HEVC** (or H.264) stream and **decodes it with hardware acceleration**
(Intel Quick Sync / D3D11VA / DXVA2), plays the phone's audio **natively** (no
extra app on the phone), and injects mouse + keyboard back as multi-touch — fast
enough for aiming.

## Features
- 🪞 **Screen mirror + native audio** — no sndcpy, nothing to install on the
  phone; audio starts and stops with the mirror automatically
- ⚡ **H.265/HEVC with hardware decoding** — lighter on the phone, smooth on the
  PC. Prefer maximum compatibility? Flip to **H.264** in the codec dropdown
- 🎯 **Mouse-look aim** + **WASD movement** + tap binds for fire / ADS / jump /
  reload / use (mouse buttons supported)
- 🎨 **F10 keymap editor** — a floating panel with a drag-and-drop palette: drop
  binds onto the *live* video, rebind keys, size the markers, tune aim
  sensitivity. **Switch keymaps on the fly** or spin up a **New** one right from
  the editor. Saves live — keep playing.
- 🎥 **F12 recording** — video + game audio + **your PC mic**, muxed into one
  file. The phone's mic stays free, so in-game **voice chat keeps working** while
  you record (with an adjustable mic-gain so your voice isn't buried).
- 🌑 **Dark theme**, and it **remembers your settings** (bitrate, resolution,
  codec, keymap, record path, mic gain…) between launches
- 📦 **Self-contained signed installer** — bundles adb, `scrcpy-server` and
  ffmpeg; nothing else to install. Built-in **Check for Updates**.

## Install
Grab the latest **Wraith-Setup** from
[Releases](https://github.com/jtiberius9-spec/Wraith/releases/latest) and run it
(per-user install, no admin needed). Launch **Wraith**, connect your phone over
USB (or wireless adb), pick a keymap, and hit **Start**.

Already on an older Wraith? Just press **Update** / **Check for Updates** — it
pulls the newest release and installs it for you.

## Quick start
1. Enable **USB debugging** on your phone and plug it in (accept the RSA prompt).
2. In Wraith's control panel: pick your device, set **bitrate / max size /
   codec**, choose a **keymap**, then hit **Start**.
3. In the mirror window, press the **switch key** (backtick `` ` `` by default,
   set per-keymap) to toggle between **GAME mode** (mouse-look + keymap) and
   **MOUSE mode** (free cursor — click to tap, type to chat).
4. Press **F10** to open the keymap editor and drag your binds onto the game's
   on-screen buttons.

## In-game keys
| Key | Action |
|-----|--------|
| **Switch key** (backtick `` ` `` by default) | Toggle GAME mode (aim + keymap) ⇄ MOUSE mode (free cursor) |
| **F10** | Keymap editor — drag binds onto the live video; switch/create keymaps; save live |
| **F12** | Start/stop recording (video + game audio + your PC mic) |
| Mouse move | Aim / camera look (GAME mode) |
| WASD | Move |
| your bound keys | Taps (fire, jump, reload, …) |

## Tuning notes
- **Set your monitor to 60 Hz** for smooth 60 fps mirroring — odd refresh rates
  (e.g. 99 Hz) don't divide evenly into 60 and will **judder**.
- **Codec**: H.265/HEVC is the default (lighter encode on the phone). If a device
  misbehaves, switch to **H.264** in the dropdown.
- **Max size**: `1920` is the sharpest a 1080p display can show; native adds
  latency for no visible gain.
- **Aim sensitivity** and marker sizes are editable live in the **F10** editor —
  drop each bind onto the game's buttons, tune, and save.
- **Recording**: set a record path first; **F12** toggles capture. Use the
  **mic-gain** control to balance your voice against the game audio.

## Built on
Wraith is a customized fork of [scrcpy](https://github.com/Genymobile/scrcpy) and
[QtScrcpy](https://github.com/barry-ran/QtScrcpy) (Apache-2.0), with native audio,
H.265 + hardware decoding, a visual drag-and-drop keymap editor, and mic-mixed
recording added on top.

## Status
**v0.5.0** — native build: native audio (no extra app), H.265 + hardware
decoding with an H.264/H.265 switch, a drag-and-drop **F10** keymap editor with
live keymap switching and new-keymap creation, **F12** recording with PC-mic
voice, a dark theme, and remembered settings. Ships as a self-contained signed
Windows installer with adb / `scrcpy-server` / ffmpeg bundled.
