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
JOY_PID = 2
TAP_PID_BASE = 10

AIM_TICK = 1 / 120          # aim engine / scheduler resolution

# Look-finger CLEAN ZONE — the central-right "black box" with no buttons in it:
# the WASD stick is off to the left, fire/ADS/scope sit on the far right, the
# stats panel is up top and the weapon bar along the bottom. The look finger
# only ever lives, travels and re-anchors INSIDE this box, so a re-press can
# never land on a button or steal the movement stick. Fractions of the
# landscape screen — nudge these if a re-anchor still clips a control.
LOOK_X0, LOOK_X1 = 0.55, 0.80    # left / right edges of the zone
LOOK_Y0, LOOK_Y1 = 0.22, 0.74    # top / bottom edges of the zone
# Vertical "home" — just inside the TOP edge. Looking DOWN (recoil) re-anchors
# here, so a spray has the WHOLE lower box to pull into and almost never
# re-anchors mid-spray = no recoil flick. (Looking UP wraps to the bottom
# instead, so you can still crane up freely.) X home is just the box centre.
LOOK_ANCHOR_X = 0.5 * (LOOK_X0 + LOOK_X1)
LOOK_ANCHOR_Y = LOOK_Y0 + 0.05
# How far the look finger may DRAG before it must re-anchor — almost the whole
# screen (it can leave the box once it's down). Big travel = rare re-anchors =
# the flick essentially never shows during normal play. Just off the edges so a
# turnaround doesn't trip an edge gesture / pull the notification shade.
TRAVEL_MX, TRAVEL_MY = 0.03, 0.06

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

        # aim — the look finger DRAGS across almost the whole screen (DF keeps it
        # as "look" once it has touched down in the clean box), so strokes are
        # long and re-anchors are RARE: normal looking and recoil never reach a
        # screen edge, so they never re-anchor and never flick. The keymap's aim
        # sensitivity is honoured; its on-screen position is unused.
        self.aim = self.km.aim_binding()
        if self.aim:
            self.aim_sens = self.aim.sensitivity or 1.0
        # TRAVEL bounds — almost the full screen, just off the edges so the
        # drag's turnaround never trips an edge gesture / the notification pull.
        self._trav_left, self._trav_right = int(TRAVEL_MX * W), int((1 - TRAVEL_MX) * W)
        self._trav_top, self._trav_bot = int(TRAVEL_MY * H), int((1 - TRAVEL_MY) * H)
        # BOX — the only place the finger may touch DOWN (re-press), so a new
        # contact never lands on a button or the WASD stick.
        self._box_left, self._box_right = int(LOOK_X0 * W), int(LOOK_X1 * W)
        self._box_top, self._box_bot = int(LOOK_Y0 * H), int(LOOK_Y1 * H)
        self._anchor_x = int(LOOK_ANCHOR_X * W)   # first-press / recoil home
        self._anchor_y = int(LOOK_ANCHOR_Y * H)
        if self.joy:                              # keep re-press clear of the stick
            self._box_left = max(self._box_left, self.joy_cx + self.joy_r)
        self._aim_down = False
        self._aim_pid = AIM_PID         # look-finger pointer id (single, like QtScrcpy)
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
        # Drain input as fast as it arrives, but only ADVANCE the aim (send a
        # phone touch) at a fixed cadence. A gaming mouse fires up to ~1000
        # events/sec; sending a touch_move for each one floods the scrcpy control
        # socket during a continuous turn and the camera goes choppy/jumpy. So we
        # COALESCE: keys/taps fire immediately (low latency), mouse deltas just
        # accumulate, and one combined look move is pushed per AIM_TICK (~120Hz).
        next_tick = time.monotonic()
        while self._run:
            now = time.monotonic()
            timeout = next_tick - now
            if timeout > 0:
                try:
                    self._handle(self.q.get(timeout=timeout))
                    continue                      # keep draining until tick is due
                except queue.Empty:
                    pass
            self._aim_tick()                      # one coalesced look move
            self._run_due()
            next_tick += AIM_TICK
            if next_tick < now:                   # fell behind -> don't burst
                next_tick = now + AIM_TICK

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
            self._aim_x, self._aim_y = self._anchor_x, self._anchor_y
            self.c.touch_down(self._aim_pid, self._aim_x, self._aim_y)
            self._aim_down = True

        self._aim_x += dx * self.aim_sens
        self._aim_y += dy * self.aim_sens

        # The finger drags across the whole SCREEN and only re-anchors when it
        # runs out of screen — so normal looking and recoil (which never reach a
        # screen edge) never re-anchor and never flick. When it does re-anchor on
        # a big continuous spin, the re-press lands back inside the clean BOX:
        #   X -> opposite side of the box in the turn direction (max room to keep
        #        sweeping that way).
        #   Y -> looking DOWN re-presses at HOME near the box top (full lower
        #        screen to spray into); looking UP re-presses at the box bottom.
        # An axis that didn't run out keeps its position (clamped into the box so
        # the touch-DOWN is always safe). Overshoot carries through so the spin
        # never loses a frame.
        out_x = not (self._trav_left < self._aim_x < self._trav_right)
        out_y = not (self._trav_top < self._aim_y < self._trav_bot)
        if out_x or out_y:
            ex = min(max(self._aim_x, self._trav_left), self._trav_right)
            ey = min(max(self._aim_y, self._trav_top), self._trav_bot)
            over_x = self._aim_x - ex                  # motion past the edge this tick
            over_y = self._aim_y - ey
            self.c.touch_move(self._aim_pid, int(ex), int(ey))  # finish to the edge
            self.c.touch_up(self._aim_pid, int(ex), int(ey))    # lift
            if out_x:                                  # re-press toward the turn dir
                nx = self._box_right if self._aim_x <= self._trav_left else self._box_left
            else:                                      # keep X, but inside the box
                nx = min(max(ex, self._box_left), self._box_right)
            if not out_y:                              # keep Y, but inside the box
                ny = min(max(ey, self._box_top), self._box_bot)
            elif self._aim_y <= self._trav_top:        # looking UP -> box bottom
                ny = self._box_bot
            else:                                      # looking DOWN (recoil) -> home
                ny = self._anchor_y
            self.c.touch_down(self._aim_pid, int(nx), int(ny))  # re-press in the box
            # _aim_down stays True — the look finger is never idle between strokes.
            self._aim_x = min(max(nx + over_x, self._trav_left), self._trav_right)
            self._aim_y = min(max(ny + over_y, self._trav_top), self._trav_bot)
            if int(self._aim_x) != int(nx) or int(self._aim_y) != int(ny):
                self.c.touch_move(self._aim_pid, int(self._aim_x), int(self._aim_y))
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
