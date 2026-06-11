"""
wraith.runtime — resolve binaries & writable paths for BOTH dev and frozen
(PyInstaller .exe) runs.

When frozen, PyInstaller unpacks bundled data under sys._MEIPASS. We ship adb,
the scrcpy-server jar and ffmpeg under a `bin/` folder there, and seed a WRITABLE
`keymaps/` folder next to the .exe (the in-window editor must be able to save).
In a normal `python -m wraith` run these fall back to the repo layout / PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Pass as creationflags= to EVERY subprocess call so a windowed (no-console)
# Wraith never spawns a flashing/persistent cmd window for adb/ffmpeg/server.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW

# the scrcpy-server jar we bundle is from scrcpy v4.0 — the server is launched
# with this exact version string, so it must match the jar.
BUNDLED_SCRCPY_VERSION = "4.0"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _bundle_root() -> Path:
    """Where bundled read-only data lives (sys._MEIPASS when frozen, repo root)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Writable dir that travels WITH the app: the .exe folder, or repo root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundled(*parts: str) -> Path | None:
    p = _bundle_root().joinpath("bin", *parts)
    return p if p.exists() else None


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name      # adb.exe vs adb (mac/linux)


def adb_path() -> str:
    b = _bundled(_exe("adb"))
    if b:
        return str(b)
    return shutil.which("adb") or "adb"


def ffmpeg_path() -> str:
    b = _bundled(_exe("ffmpeg"))
    if b:
        return str(b)
    return shutil.which("ffmpeg") or "ffmpeg"


def server_jar() -> Path | None:
    """Bundled scrcpy-server jar, if we shipped one."""
    return _bundled("scrcpy-server")


def icon_ico() -> Path | None:
    p = _bundle_root() / "wraith.ico"
    return p if p.exists() else None


def icon_png() -> Path | None:
    p = _bundle_root() / "wraith_icon_preview.png"
    return p if p.exists() else None


def keymaps_dir() -> Path:
    """Writable keymaps folder next to the app, seeded from the bundle once."""
    d = app_dir() / "keymaps"
    d.mkdir(parents=True, exist_ok=True)
    if is_frozen():
        src = _bundle_root() / "keymaps"
        if src.is_dir():
            for f in src.glob("*.json"):
                tgt = d / f.name
                if not tgt.exists():
                    try:
                        shutil.copy2(f, tgt)
                    except OSError:
                        pass
    return d
