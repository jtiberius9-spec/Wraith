"""
wraith.injector — translates captured keyboard/mouse input into scrcpy touches.

Faithful to QtScrcpy's InputConvertGame model:
  * a planted "look" finger that pans across the screen and only re-anchors at
    the screen margins (long continuous travel, no micro-stutter),
  * a steer-wheel "move" finger held at the joystick center and dragged by the
    direction offsets,
  * tap / click-twice / multi-click / drag action nodes.

A single worker thread drains an event queue, ticks the aim engine, and runs a
non-blocking scheduler so timed gestures (multi-click macros, drags, tap
release) NEVER sleep the worker — aim and movement stay live throughout.

Pointer-id allocation (each is an independent "finger"):
    1   -> aim / camera-look
    2   -> movement joystick (WASD)
    10+ -> individual tap / action bindings
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from .control import ScrcpyControl
from .keymap import Keymap, Binding

log = logging.getLogger("wraith.injector")

AIM_PID = 1
AIM_PID_ALT = 3             # look finger alternates 1<->3 across re-anchors so a
                           # relative-look game (DF) sees each re-press as a NEW
                           # finger with no prior position -> no recenter "flick"
JOY_PID = 2
TAP_PID_BASE = 10

AIM_TICK = 1 / 120          # aim engine / scheduler resolution
AIM_MARGIN = 0.05           # screen headroom each side. Smaller = more look-finger
                            # travel before a re-anchor (DF can't fully hide the
                            # recenter, so fewer re-anchors = fewer recoil flicks)
TAP_HOLD = 0.035            # quick-tap finger-down duration (s)


class Injector:
    def __init__(self, control: ScrcpyControl, keymap: Keymap):
        self.c = control
        self.km = keymap
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self._run = False
        self._thread: threading.Thread | None = None

        # non-blocking timed-action scheduler: list of [deadline, fn]
        self._sched: list[list] = []

        # --- precompute pixel anchors -----------------------------------
        W, H = control.width, control.height
        self.W, self.H = W, H

        # tap/action bindings: key -> list[(pid, binding, x, y)]
        self.taps: dict[str, list[tuple[int, Binding, int, int]]] = {}
        for i, b in enumerate(self.km.taps()):
            x, y = control.norm_to_px(b.nx, b.ny)
            self.taps.setdefault(b.key, []).append((TAP_PID_BASE + i, b, x, y))
        self._tap_down: dict[tuple[str, int], tuple[int, int, int]] = {}

        # joystick
        self.joy = self.km.joystick()
        if self.joy:
            self.joy_cx, self.joy_cy = control.norm_to_px(self.joy.nx, self.joy.ny)
            self.joy_r = int(self.joy.radius * W)
            off = self.joy.offsets or {}
            self.joy_offsets = {
                "left": int(float(off.get("left", self.joy.radius)) * W),
                "right": int(float(off.get("right", self.joy.radius)) * W),
                "up": int(float(off.get("up", self.joy.radius)) * H),
                "down": int(float(off.get("down", self.joy.radius)) * H),
            }
        self._joy_keys: set[str] = set()
        self._joy_down = False

        # aim — QtScrcpy keeps the look finger planted and only re-anchors at
        # the screen margins, giving long, smooth, continuous turns.
        self.aim = self.km.aim_binding()
        if self.aim:
            self.aim_cx, self.aim_cy = control.norm_to_px(self.aim.nx, self.aim.ny)
            self.aim_sens = self.aim.sensitivity or 1.0
        mx, my = int(AIM_MARGIN * W), int(AIM_MARGIN * H)
        self._aim_left, self._aim_right = mx, W - mx
        self._aim_top, self._aim_bot = my, H - my
        self._aim_down = False
        self._aim_pid = AIM_PID         # current look-finger id (alternates on re-anchor)
        self._recenter = None           # where the next re-press lands (set at re-anchor)
        self._aim_x = 0
        self._aim_y = 0
        self._aim_pending = [0.0, 0.0]

    # -- public: called from listener threads --------------------------------
    def feed_key(self, name: str, pressed: bool) -> None:
        self.q.put(("key", name, pressed))

    def feed_button(self, name: str, pressed: bool) -> None:
        self.q.put(("key", name, pressed))   # mouse buttons share the tap path

    def feed_mouse_delta(self, dx: float, dy: float) -> None:
        self.q.put(("mdelta", dx, dy))

    def release_all(self) -> None:
        """Lift every synthetic finger (handled on the worker thread). Call when
        leaving GAME mode: a planted aim/joystick finger left down turns the
        next real mouse tap into a two-finger PINCH — e.g. the DF tactical map
        zooming instead of placing a barrage marker."""
        self.q.put(("release",))

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self._release_all()

    # -- scheduler (non-blocking timed gestures) ------------------------------
    def _schedule(self, delay: float, fn) -> None:
        self._sched.append([time.monotonic() + delay, fn])

    def _run_due(self) -> None:
        if not self._sched:
            return
        now = time.monotonic()
        ready = [e for e in self._sched if e[0] <= now]
        if not ready:
            return
        self._sched = [e for e in self._sched if e[0] > now]
        for _, fn in ready:           # insertion order; deadlines are monotonic
            try:
                fn()
            except Exception as exc:  # pragma: no cover
                log.warning("scheduled action: %s", exc)

    # -- worker ---------------------------------------------------------------
    def _loop(self) -> None:
        while self._run:
            try:
                ev = self.q.get(timeout=AIM_TICK)
                self._handle(ev)
            except queue.Empty:
                pass
            self._aim_tick()
            self._run_due()

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "key":
            self._on_key(ev[1], ev[2])
        elif kind == "mdelta":
            if self.aim:
                self._aim_pending[0] += ev[1]
                self._aim_pending[1] += ev[2]
        elif kind == "release":
            self._release_all()

    # -- keys / taps / joystick ----------------------------------------------
    def _on_key(self, name: str, pressed: bool) -> None:
        # joystick movement keys
        if self.joy and name in self.joy.keys.values():
            if pressed:
                self._joy_keys.add(name)
            else:
                self._joy_keys.discard(name)
            self._update_joystick()
            return

        # tap bindings
        if name in self.taps:
            for idx, (pid, binding, x, y) in enumerate(self.taps[name]):
                self._handle_binding(name, idx, pid, binding, x, y, pressed)

    def _handle_binding(self, name: str, idx: int, pid: int, b: Binding,
                        x: int, y: int, pressed: bool) -> None:
        key = (name, idx)
        if b.action == "click_twice":
            if pressed:
                self._tap(pid, x, y)
                self._schedule(0.09, lambda: self._tap(pid, x, y))
            return
        if b.action == "click_multi":
            if pressed:
                self._click_multi(pid, b)
            return
        if b.action == "drag":
            if pressed:
                self._drag(pid, b)
            return

        if pressed:
            if key not in self._tap_down:
                self.c.touch_down(pid, x, y)
                self._tap_down[key] = (pid, x, y)
                if not b.hold:
                    self.c.touch_up(pid, x, y)
                    self._tap_down.pop(key, None)
        else:
            if key in self._tap_down:
                down_pid, dx, dy = self._tap_down.pop(key)
                self.c.touch_up(down_pid, dx, dy)

    def _tap(self, pid: int, x: int, y: int, hold: float = TAP_HOLD) -> None:
        """Quick down+up, non-blocking (up is scheduled, not slept)."""
        self.c.touch_down(pid, x, y)
        self._schedule(hold, lambda: self.c.touch_up(pid, x, y))

    def _click_multi(self, pid: int, b: Binding) -> None:
        """Replay a QtScrcpy KMT_CLICK_MULTI macro without blocking the worker."""
        t = 0.0
        for i, node in enumerate(sorted(b.clicks, key=lambda n: n.get("order", 0))):
            t += float(node.get("delay", 0) or 0) / 1000.0
            pos = node.get("pos", {})
            x, y = self.c.norm_to_px(float(pos.get("x", b.nx)),
                                     float(pos.get("y", b.ny)))
            p = pid + (i % 4)         # a few rotating fingers so taps don't collide
            self._schedule(t, lambda p=p, x=x, y=y: self.c.touch_down(p, x, y))
            self._schedule(t + TAP_HOLD,
                           lambda p=p, x=x, y=y: self.c.touch_up(p, x, y))

    def _drag(self, pid: int, b: Binding) -> None:
        """Timed swipe from start->end, steps scheduled (never slept)."""
        sx, sy = self.c.norm_to_px(b.start_nx, b.start_ny)
        ex, ey = self.c.norm_to_px(b.end_nx, b.end_ny)
        self.c.touch_down(pid, sx, sy)
        steps = max(3, int(12 / max(0.1, min(b.drag_speed, 1.0))))
        base = (b.start_delay or 0) / 1000.0
        for i in range(1, steps + 1):
            t = base + i * 0.008
            mx = int(sx + (ex - sx) * i / steps)
            my = int(sy + (ey - sy) * i / steps)
            self._schedule(t, lambda mx=mx, my=my: self.c.touch_move(pid, mx, my))
        self._schedule(base + steps * 0.008 + 0.01,
                       lambda: self.c.touch_up(pid, ex, ey))

    def _update_joystick(self) -> None:
        if not self.joy:
            return
        k = self.joy.keys
        vx = (1 if k.get("right") in self._joy_keys else 0) - \
             (1 if k.get("left") in self._joy_keys else 0)
        vy = (1 if k.get("down") in self._joy_keys else 0) - \
             (1 if k.get("up") in self._joy_keys else 0)

        if vx == 0 and vy == 0:
            if self._joy_down:
                self.c.touch_up(JOY_PID, self.joy_cx, self.joy_cy)
                self._joy_down = False
            return

        ox = self.joy_offsets["right"] if vx > 0 else self.joy_offsets["left"]
        oy = self.joy_offsets["down"] if vy > 0 else self.joy_offsets["up"]
        dx_off = vx * ox
        dy_off = vy * oy
        if vx and vy:
            # diagonal (e.g. W+A strafe): a full ox on EACH axis lands ~1.41x
            # past the joystick ring, which DF clamps back to a single axis ->
            # "just goes left, no strafe". Scale to the ring so both axes hold.
            dx_off = int(dx_off * 0.70710678)
            dy_off = int(dy_off * 0.70710678)
        tx = self.joy_cx + dx_off
        ty = self.joy_cy + dy_off
        if not self._joy_down:
            self.c.touch_down(JOY_PID, self.joy_cx, self.joy_cy)
            self._joy_down = True
        self.c.touch_move(JOY_PID, tx, ty)

    # -- aim / mouse-look -----------------------------------------------------
    def _aim_tick(self) -> None:
        if not self.aim:
            return
        dx, dy = self._aim_pending
        if not (dx or dy):
            # mouse still -> keep the look finger planted (QtScrcpy behaviour:
            # the camera simply stops; no lift, no re-anchor jump on resume).
            return
        self._aim_pending[0] = 0.0
        self._aim_pending[1] = 0.0

        if not self._aim_down:
            # re-press where the last re-anchor decided (opposite the edge we
            # exited, for max continued travel), else the keymap anchor.
            self._aim_x, self._aim_y = self._recenter or (self.aim_cx, self.aim_cy)
            self._recenter = None
            self.c.touch_down(self._aim_pid, self._aim_x, self._aim_y)
            self._aim_down = True

        self._aim_x += dx * self.aim_sens
        self._aim_y += dy * self.aim_sens

        # Re-anchor only at the screen margins (long continuous travel). On the
        # edge we slide to the boundary, lift, and the next motion re-presses at
        # the start anchor — infinite mouse travel without mid-screen stutter.
        if not (self._aim_left < self._aim_x < self._aim_right and
                self._aim_top < self._aim_y < self._aim_bot):
            cx = int(min(max(self._aim_x, self._aim_left), self._aim_right))
            cy = int(min(max(self._aim_y, self._aim_top), self._aim_bot))
            # Slide to the edge, lift — and let the NEXT motion re-press at the
            # anchor (the `if not self._aim_down` block at the top). One planted
            # finger between re-anchors, so within-bounds sensitivity is the
            # snappy 1:1 of the pre-0.4.0 build (no 45ms park, no in-tick
            # re-press). The flick was DF reading the edge->center recenter as
            # the SAME finger jumping back; alternating the pointer id (below)
            # makes each re-press a brand-new finger it can't delta against.
            self.c.touch_move(self._aim_pid, cx, cy)
            self.c.touch_up(self._aim_pid, cx, cy)
            self._aim_down = False
            # Re-press OPPOSITE the edge we exited so a sustained drag in that
            # direction (recoil pull-down, a long turn) gets the FULL box again
            # before the next re-anchor — far fewer recenters per spray/turn,
            # which is what reduces visible flicks. Axes not exited stay centred.
            rx = self._aim_left + 2 if self._aim_x >= self._aim_right else \
                 self._aim_right - 2 if self._aim_x <= self._aim_left else self.aim_cx
            ry = self._aim_top + 2 if self._aim_y >= self._aim_bot else \
                 self._aim_bot - 2 if self._aim_y <= self._aim_top else self.aim_cy
            self._recenter = (rx, ry)
            # next stroke re-presses under the OTHER id (new finger, no history)
            self._aim_pid = AIM_PID_ALT if self._aim_pid == AIM_PID else AIM_PID
        else:
            self.c.touch_move(self._aim_pid, int(self._aim_x), int(self._aim_y))

    # -- cleanup --------------------------------------------------------------
    def _release_all(self) -> None:
        try:
            for _, (pid, x, y) in list(self._tap_down.items()):
                self.c.touch_up(pid, x, y)
            self._tap_down.clear()
            self._joy_keys.clear()      # stale held keys would re-pin the wheel
            if self._joy_down:
                self.c.touch_up(JOY_PID, self.joy_cx, self.joy_cy)
                self._joy_down = False
            if self._aim_down:
                self.c.touch_up(self._aim_pid, int(self._aim_x), int(self._aim_y))
                self._aim_down = False
            self._aim_pending[0] = self._aim_pending[1] = 0.0
            self._sched.clear()
        except Exception as exc:  # pragma: no cover
            log.warning("release_all: %s", exc)
