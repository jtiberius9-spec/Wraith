"""
wraith.control — low-level scrcpy control-socket client.

We do NOT decode video here. Real `scrcpy` shows the mirror window (hardware
accelerated, audio included). Wraith starts its OWN control-only scrcpy-server
instance and speaks scrcpy's binary control protocol to inject multi-touch
MotionEvents — fast enough for FPS aim.

Protocol reference: scrcpy 4.0 control_msg.c (INJECT_TOUCH_EVENT, 32 bytes).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

log = logging.getLogger("wraith.control")

# --- scrcpy control message types -------------------------------------------
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_BACK_OR_SCREEN_ON = 4

# --- Android MotionEvent actions ---------------------------------------------
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# --- Android MotionEvent buttons ---------------------------------------------
BUTTON_PRIMARY = 1 << 0      # left mouse / main touch

# Pressure is sent as a 16-bit fixed-point value (0.0 -> 0x0000, 1.0 -> 0xFFFF).
_PRESS_MAX = 0xFFFF


def _u16fp(value: float) -> int:
    if value <= 0.0:
        return 0
    if value >= 1.0:
        return _PRESS_MAX
    return int(value * _PRESS_MAX)


class AdbError(RuntimeError):
    pass


def _adb_base(serial: str | None) -> list[str]:
    from .runtime import adb_path
    return [adb_path()] + (["-s", serial] if serial else [])


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    from .runtime import NO_WINDOW
    log.debug("run: %s", " ".join(cmd))
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise AdbError(f"{' '.join(cmd)} -> {out.returncode}\n{out.stderr.strip()}")
    return out.stdout.strip()


def detect_scrcpy_version() -> str:
    """Return the scrcpy version string the server jar expects.

    Frozen builds ship their own scrcpy-server jar (no scrcpy.exe), so we use
    the known bundled version and never spawn a missing process."""
    from .runtime import server_jar, BUNDLED_SCRCPY_VERSION
    if server_jar() is not None:
        return BUNDLED_SCRCPY_VERSION
    try:
        from .runtime import NO_WINDOW
        first = subprocess.run(
            ["scrcpy", "--version"], capture_output=True, text=True, timeout=8,
            creationflags=NO_WINDOW
        ).stdout.splitlines()[0]
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", first)
        if m:
            return m.group(1)
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("could not detect scrcpy version: %s", exc)
    return "4.0"


def find_server_jar() -> Path:
    # 0) jar bundled inside the frozen app (the finished .exe path)
    from .runtime import server_jar
    b = server_jar()
    if b is not None:
        return b

    # 1) explicit override (scrcpy itself honours this var too)
    env = os.environ.get("SCRCPY_SERVER_PATH")
    if env and Path(env).is_file():
        return Path(env)

    # 2) next to the scrcpy executable (Windows zip/scoop/choco, Linux)
    exe = shutil.which("scrcpy")
    if exe:
        d = Path(exe).resolve().parent
        for name in ("scrcpy-server", "scrcpy-server.jar"):
            if (d / name).is_file():
                return d / name

    # 3) well-known install locations (macOS brew, Linux, Windows)
    candidates = [
        "/opt/homebrew/opt/scrcpy/share/scrcpy/scrcpy-server",
        "/opt/homebrew/share/scrcpy/scrcpy-server",
        "/usr/local/share/scrcpy/scrcpy-server",
        "/usr/share/scrcpy/scrcpy-server",
        os.path.expandvars(r"%LOCALAPPDATA%\scrcpy\scrcpy-server"),
        os.path.expandvars(r"%ProgramFiles%\scrcpy\scrcpy-server"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)

    # 4) last resort on macOS: ask brew
    try:
        prefix = _run(["brew", "--prefix", "scrcpy"])
        p = Path(prefix) / "share" / "scrcpy" / "scrcpy-server"
        if p.is_file():
            return p
    except Exception:
        pass

    raise FileNotFoundError(
        "scrcpy-server not found. Install scrcpy, or set SCRCPY_SERVER_PATH "
        "to the full path of the 'scrcpy-server' file."
    )


class ScrcpyControl:
    """Owns a control-only scrcpy-server instance + the control socket."""

    REMOTE_JAR = "/data/local/tmp/wraith-scrcpy-server.jar"

    def __init__(self, serial: str | None = None, port: int = 27199):
        self.serial = serial
        self.port = port
        self.scid = "0a1b2c3d"  # static dev id; fine for single instance
        self.version = detect_scrcpy_version()
        self.sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self.alive = False          # True once the control socket is usable
        # device landscape resolution (W>H). Filled by connect().
        self.width = 0
        self.height = 0

    # -- device helpers -------------------------------------------------------
    def _device_resolution(self) -> tuple[int, int]:
        out = _run(_adb_base(self.serial) + ["shell", "wm", "size"])
        # "Physical size: 1080x2400" (and maybe an Override size line)
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise AdbError(f"could not parse `wm size`: {out!r}")
        w, h = (int(m[-1][0]), int(m[-1][1]))
        # store as LANDSCAPE (game orientation): width >= height
        return (max(w, h), min(w, h))

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        base = _adb_base(self.serial)
        jar = find_server_jar()
        log.info("pushing server jar (%s, scrcpy %s)", jar, self.version)
        _run(base + ["push", str(jar), self.REMOTE_JAR])

        # forward tunnel: server LISTENS on device abstract socket, we connect.
        _run(base + ["forward", f"tcp:{self.port}", f"localabstract:scrcpy_{self.scid}"])

        server_cmd = base + [
            "shell",
            f"CLASSPATH={self.REMOTE_JAR}",
            "app_process", "/", "com.genymobile.scrcpy.Server",
            self.version,
            f"scid={self.scid}",
            "log_level=info",
            "tunnel_forward=true",
            "control=true",
            "video=false",
            "audio=false",
            "cleanup=true",
        ]
        log.info("starting control-only scrcpy-server on device")
        from .runtime import NO_WINDOW
        self._proc = subprocess.Popen(
            server_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=NO_WINDOW
        )

        self._connect_socket()
        self.width, self.height = self._device_resolution()
        log.info("control socket up. device landscape res = %dx%d",
                 self.width, self.height)

        # View-only mirror can't use scrcpy --stay-awake, so keep the device
        # awake while on USB ourselves (best-effort).
        try:
            _run(base + ["shell", "svc", "power", "stayon", "usb"])
        except Exception as exc:
            log.debug("svc power stayon failed (non-fatal): %s", exc)

    def _connect_socket(self, attempts: int = 50) -> None:
        last = None
        for i in range(attempts):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # keepalive so a peer that vanishes (USB suspend, device doze or
                # reboot during a long idle/minimized session) is detected by the
                # OS instead of only surfacing as a write error on the next tap.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # tunnel_forward -> server sends a dummy byte on the first socket
                dummy = s.recv(1)
                if dummy == b"":
                    s.close()
                    raise ConnectionError("empty dummy byte")
                self.sock = s
                self.alive = True
                return
            except OSError as exc:
                last = exc
                time.sleep(0.1)
        raise AdbError(f"could not connect to control socket: {last}")

    def close(self) -> None:
        self.alive = False
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        try:
            _run(_adb_base(self.serial) + ["forward", "--remove", f"tcp:{self.port}"])
        except Exception:
            pass

    # -- connection health ----------------------------------------------------
    def _mark_dead(self, exc: Exception | None = None) -> None:
        """Tear down a control socket that errored. Logged once per drop so a
        flurry of failing taps doesn't spam the log."""
        if self.alive and exc is not None:
            log.warning("control socket lost: %s", exc)
        self.alive = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def reconnect(self) -> bool:
        """Best-effort revival after an idle/USB drop: rebuild the server +
        socket. Works when the DEVICE is still present (USB hiccup, doze); a
        full reboot needs a fresh session (video stream is gone too). Returns
        self.alive."""
        try:
            self.close()
        except Exception:
            pass
        try:
            self.start()
        except Exception as exc:
            log.warning("control reconnect failed: %s", exc)
            self.alive = False
        return self.alive

    # -- touch injection ------------------------------------------------------
    def _send_touch(self, action: int, pointer_id: int, x: int, y: int,
                    pressure: float, buttons: int) -> bool:
        """Inject one MotionEvent. Returns False (never raises) if the control
        link is down, so a vanished phone degrades gracefully instead of
        crashing the app on the next tap."""
        if not self.sock:
            return False
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        msg = struct.pack(
            ">BBqiiHHHii",
            TYPE_INJECT_TOUCH_EVENT,
            action,
            pointer_id & 0xFFFFFFFFFFFFFFFF,
            x, y,
            self.width, self.height,
            _u16fp(pressure),
            0,            # action_button
            buttons,
        )
        try:
            self.sock.sendall(msg)
            return True
        except OSError as exc:
            self._mark_dead(exc)
            return False

    def touch_down(self, pointer_id: int, x: int, y: int) -> bool:
        return self._send_touch(ACTION_DOWN, pointer_id, x, y, 1.0, BUTTON_PRIMARY)

    def touch_move(self, pointer_id: int, x: int, y: int) -> bool:
        return self._send_touch(ACTION_MOVE, pointer_id, x, y, 1.0, BUTTON_PRIMARY)

    def touch_up(self, pointer_id: int, x: int, y: int) -> bool:
        return self._send_touch(ACTION_UP, pointer_id, x, y, 0.0, 0)

    def tap(self, pointer_id: int, x: int, y: int, hold: float = 0.0) -> None:
        self.touch_down(pointer_id, x, y)
        if hold:
            time.sleep(hold)
        self.touch_up(pointer_id, x, y)

    # -- convenience: coords are normalized (0..1) over landscape screen ------
    def norm_to_px(self, nx: float, ny: float) -> tuple[int, int]:
        return (int(nx * self.width), int(ny * self.height))
