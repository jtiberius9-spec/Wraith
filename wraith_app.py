"""
Wraith — single frozen-exe entry point.

PyInstaller produces ONE executable. We dispatch on argv so the same .exe is
both the launcher (default, double-click) and the mirror window (spawned by the
launcher as `Wraith.exe mirror --serial ... --keymap ...`).
"""

import multiprocessing
import os
import sys


def _setup_io():
    """Windowed (console=False) frozen builds have sys.stdout/err = None, which
    makes the app's many print() calls raise. Route them to a log next to the
    .exe (falls back to the null device if that folder isn't writable)."""
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from wraith.runtime import app_dir
        f = open(app_dir() / "wraith.log", "a", buffering=1, encoding="utf-8")
    except Exception:
        f = open(os.devnull, "w")
    sys.stdout = sys.stderr = f


def main():
    multiprocessing.freeze_support()        # safe no-op; guards child spawns
    _setup_io()
    argv = sys.argv[1:]
    if argv and argv[0] == "mirror":
        from wraith.mirror import cli
        cli(argv[1:])
    else:
        from wraith.launcher import main as launcher_main
        launcher_main()


if __name__ == "__main__":
    main()
