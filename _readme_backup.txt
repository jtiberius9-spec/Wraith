WRAITH — standalone Windows build
==================================

HOW TO RUN
  Double-click  Wraith.exe   (or use the Start Menu / desktop shortcut if you
  installed via the Setup installer).

Everything is bundled — you do NOT need Python, scrcpy, adb, or ffmpeg
installed. This folder is fully self-contained.

WHAT IT DOES
  Mirrors your Android phone (USB debugging on) to a window on this PC, with an
  FPS keymapper. The control panel (QtScrcpy-style) lets you pick device,
  bitrate, size, fps, keymap, record path, and options, with a live adb console.
  Double-click a device, or hit Start.

IN THE MIRROR WINDOW
  CTRL  - toggle GAME mode (aim/keymap) vs MOUSE mode (free cursor)
  F10   - open the drag-and-drop keymap editor (edit & apply live)
  F12   - record video + audio to your chosen save path
  F9    - quit

  PORTRAIT SIDEBAR: when the phone is upright (mouse mode), a nav bar appears
  on the left of the mirror — power, volume +/-, screen on/off, notifications,
  recents, home, back. It hides automatically when a game goes landscape.
  (Toggle it off with the "show toolbar" box in the launcher.)

KEYMAPS
  Live in the  keymaps\  folder next to Wraith.exe. Drop QtScrcpy .json files
  there, or make your own with the F10 editor. They're yours to edit/save.

NOTES
  - First launch may be a touch slow (Windows scans the new .exe). Fine after.
  - Keep this whole folder together. The thing you run is Wraith.exe; the
    _internal folder holds the bundled libraries and binaries.
  - Trouble? A log is written to  wraith.log  next to Wraith.exe.

Requirements on the PHONE: USB debugging enabled, plugged in via USB.
