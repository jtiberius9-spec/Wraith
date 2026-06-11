"""
wraith.editor — in-window drag-and-drop keymap editor (ScrcpyKeyMapper-style).

F10 in the mirror window toggles EDIT MODE:
  * the window grows a sidebar; the live video keeps playing underneath
    (FREEZE FRAME holds the current frame — like editing over a screenshot),
  * drag a binding type from the sidebar onto the video, press the key (or
    mouse button) to bind it, drag markers to reposition, mouse-wheel to tune
    radius / sensitivity / drag speed,
  * SAVE writes the keymap JSON and re-applies it to the running injector
    immediately — keep playing with the new layout, no restart.

All positions are stored NORMALIZED (0..1 over the video frame), same model as
keymap.py, so edits stay valid at any window size / device resolution.
"""

from __future__ import annotations

import time
from pathlib import Path

import pygame

from .keymap import Binding

# palette: (kind id, label, hint)
PALETTE = [
    ("tap",      "TAP",        "press key -> touch"),
    ("tap2",     "DOUBLE TAP", "two quick taps"),
    ("multi",    "MULTI TAP",  "macro: timed taps"),
    ("drag",     "DRAG",       "swipe start -> end"),
    ("joystick", "JOYSTICK",   "WASD steer wheel"),
    ("aim",      "AIM",        "mouse-look anchor"),
]

COLORS = {
    "tap":      (66, 133, 244),
    "tap2":     (171, 71, 188),
    "multi":    (255, 167, 38),
    "drag":     (38, 198, 218),
    "joystick": (102, 187, 106),
    "aim":      (239, 83, 80),
}

SIDE_BG = (24, 26, 30)
SIDE_EDGE = (60, 64, 72)
ITEM_BG = (38, 41, 47)
TEXT = (232, 234, 237)
DIM = (154, 160, 166)
SEL = (255, 255, 255)
GREEN = (24, 128, 56)
OK = (129, 201, 149)


def _kind(b: Binding) -> str:
    if b.type == "joystick":
        return "joystick"
    if b.type == "aim":
        return "aim"
    return {"click_twice": "tap2", "click_multi": "multi",
            "drag": "drag"}.get(b.action, "tap")


def _cl(v: float) -> float:
    return max(0.0, min(1.0, v))


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class KeymapEditor:
    SIDEBAR_W = 200

    def __init__(self, keymap, path, *, norm_key, get_frame, on_apply):
        self.km = keymap
        self.path = Path(path)
        self.norm_key = norm_key          # pygame keycode -> wraith key name
        self.get_frame = get_frame        # () -> latest RGB ndarray or None
        self.on_apply = on_apply          # called after save -> rebuild injector

        pygame.font.init()
        self.f = pygame.font.SysFont("Segoe UI", 14)
        self.fb = pygame.font.SysFont("Segoe UI", 14, bold=True)
        self.fs = pygame.font.SysFont("Segoe UI", 11)

        self.selected: Binding | None = None
        self.capturing = False            # next key/button -> selected.key
        self.capturing_switch = False     # next key -> keymap.switch_key
        self.naming = False               # typing a name for a NEW keymap
        self.name_buf = ""
        self.frozen = False
        self.frozen_arr = None
        self.dirty = False

        self._drag_new: str | None = None        # palette kind being dragged
        self._drag_bind = None                    # (binding, part, last_norm)
        self._mouse = (0, 0)
        self._last_click = (0.0, None)            # (time, binding) for dbl-click
        self._msg = ""
        self._msg_t = 0.0
        self._ui: dict[str, pygame.Rect] = {}     # widget id -> rect (set in draw)
        self._files: list[str] = []               # keymaps/*.json (click to apply)
        self._files_t = 0.0
        self._km_scroll = 0                       # first visible keymap row
        self._km_visible = self.KM_ROWS           # rows that fit (set each draw)
        self._pending_load = ("", 0.0)            # discard-confirm for dirty edits

    # -- public ---------------------------------------------------------------
    def say(self, msg: str) -> None:
        self._msg, self._msg_t = msg, time.monotonic()

    def handle(self, ev, vr: pygame.Rect) -> bool:
        """Process one pygame event. Returns True if the editor consumed it
        (mirror then skips its own shortcuts like F9/F12 for that event)."""
        if ev.type == pygame.KEYDOWN:
            return self._on_key(ev)
        if ev.type == pygame.MOUSEMOTION:
            self._mouse = ev.pos
            if self._drag_bind:
                self._move_drag(ev.pos, vr)
            return True
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 2, 3):
            return self._on_mouse_down(ev, vr)
        if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            return self._on_mouse_up(ev, vr)
        if ev.type == pygame.MOUSEWHEEL:
            self._on_wheel(ev.y, vr)
            return True
        return False

    def save(self) -> None:
        try:
            self.km.save(self.path)
        except Exception as exc:
            self.say(f"save failed: {exc}")
            return
        self.dirty = False
        self.say(f"saved {self.path.name} — live!")
        if self.on_apply:
            self.on_apply()

    # -- keyboard -------------------------------------------------------------
    def _on_key(self, ev) -> bool:
        name = self.norm_key(ev.key)
        if self.naming:                    # typing a filename for a NEW keymap
            if name == "enter":
                self.naming = False
                self._create_new(self.name_buf)
            elif name == "esc":
                self.naming = False
                self.say("new keymap cancelled")
            elif name == "backspace":
                self.name_buf = self.name_buf[:-1]
            elif ev.unicode and (ev.unicode.isalnum() or ev.unicode in " -_") \
                    and len(self.name_buf) < 28:
                self.name_buf += ev.unicode
            return True
        if self.capturing_switch:
            self.capturing_switch = False
            if name != "esc":
                self.km.switch_key = name
                self.dirty = True
                self.say(f"switch key = {name.upper()}")
            return True
        if self.capturing and self.selected is not None:
            self.capturing = False
            if name == "esc":
                self.say("capture cancelled")
            else:
                self._assign_key(self.selected, name)
            return True

        if name == "delete" and self.selected is not None:
            try:
                self.km.bindings.remove(self.selected)
            except ValueError:
                pass
            self.selected = None
            self.dirty = True
            self.say("binding deleted")
            return True
        if name == "backspace" and self.selected is not None:
            b = self.selected
            if b.type == "tap" and b.action == "click_multi" and len(b.clicks) > 1:
                b.clicks.pop()
                self.dirty = True
                self.say(f"{len(b.clicks)} points")
                return True
            return False
        if name == "h" and self.selected is not None \
                and self.selected.type == "tap" and self.selected.action == "hold":
            self.selected.hold = not self.selected.hold
            self.dirty = True
            self.say("hold ON" if self.selected.hold else "hold OFF (quick tap)")
            return True
        if name == "esc" and self.selected is not None:
            self.selected = None
            return True
        if name == "s" and (ev.mod & pygame.KMOD_CTRL):
            self.save()
            return True
        return False

    def _assign_key(self, b: Binding, name: str) -> None:
        cleared = False
        for o in self.km.taps():
            if o is not b and o.key == name:
                o.key = ""               # steal the key; old binding shows "?"
                cleared = True
        b.key = name
        self.dirty = True
        self.say(f"bound {name.upper()}"
                 + (" (taken from another binding)" if cleared else ""))

    # -- mouse ----------------------------------------------------------------
    def _on_mouse_down(self, ev, vr: pygame.Rect) -> bool:
        pos = ev.pos
        if self.capturing_switch:
            self.capturing_switch = False        # switch key must be a key
        if self.capturing and self.selected is not None:
            if vr.collidepoint(pos):
                btn = {1: "mouse_left", 2: "mouse_middle", 3: "mouse_right"}[ev.button]
                self._assign_key(self.selected, btn)
                self.capturing = False
                return True
            self.capturing = False               # sidebar click cancels capture

        if not vr.collidepoint(pos):             # sidebar
            if ev.button == 1:
                self._sidebar_click(pos)
            return True

        if ev.button == 3:                       # right-click: add MULTI point
            b = self.selected
            if b is not None and b.type == "tap" and b.action == "click_multi":
                nx, ny = self._to_norm(pos, vr)
                b.clicks.append({"delay": 50,
                                 "pos": {"x": round(nx, 4), "y": round(ny, 4)},
                                 "order": len(b.clicks) + 1})
                self.dirty = True
                self.say(f"point {len(b.clicks)} added")
            return True
        if ev.button != 1:
            return True

        hit = self._hit_test(pos, vr)
        now = time.monotonic()
        if hit:
            b, part = hit
            lt, lb = self._last_click
            self.selected = b
            if lb is b and (now - lt) < 0.35 and b.type == "tap":
                self.capturing = True            # dbl-click = rebind
                self.say("press a key / mouse button…")
            else:
                self._drag_bind = (b, part, self._to_norm(pos, vr))
            self._last_click = (now, b)
        else:
            self.selected = None
            self._last_click = (now, None)
        return True

    def _on_mouse_up(self, ev, vr: pygame.Rect) -> bool:
        if self._drag_new:
            kind = self._drag_new
            self._drag_new = None
            if vr.collidepoint(ev.pos):
                nx, ny = self._to_norm(ev.pos, vr)
                self._create(kind, round(nx, 4), round(ny, 4))
            return True
        self._drag_bind = None
        return True

    def _on_wheel(self, y: int, vr: pygame.Rect) -> None:
        if self._mouse[0] >= vr.right:           # over the sidebar -> scroll list
            hi = max(0, len(self._files) - self._km_visible)
            self._km_scroll = max(0, min(self._km_scroll - y, hi))
            return
        b = self.selected
        if b is None or not y:
            return
        f = 1.06 ** y
        if b.type == "joystick":
            off = b.offsets or {"left": b.radius, "right": b.radius,
                                "up": b.radius, "down": b.radius}
            b.offsets = {k: max(0.02, min(0.45, float(v) * f)) for k, v in off.items()}
            b.radius = max(b.offsets.values())
            self.say(f"joystick size {b.radius:.3f}")
        elif b.type == "aim":
            b.sensitivity = max(0.05, min(10.0, (b.sensitivity or 1.0) * f))
            self.say(f"aim sensitivity ×{b.sensitivity:.2f}")
        elif b.type == "tap" and b.action == "drag":
            b.drag_speed = max(0.1, min(4.0, (b.drag_speed or 1.0) * f))
            self.say(f"drag speed ×{b.drag_speed:.2f}")
        else:
            return
        self.dirty = True

    # -- create / move ----------------------------------------------------------
    def _create(self, kind: str, nx: float, ny: float) -> None:
        # injector supports ONE joystick and ONE aim anchor -> move the existing
        if kind == "joystick" and self.km.joystick() is not None:
            b = self.km.joystick()
            b.nx, b.ny = nx, ny
            self.selected, self.dirty = b, True
            self.say("moved the existing joystick (only one)")
            return
        if kind == "aim" and self.km.aim_binding() is not None:
            b = self.km.aim_binding()
            b.nx, b.ny = nx, ny
            self.selected, self.dirty = b, True
            self.say("moved the existing aim anchor (only one)")
            return

        if kind == "joystick":
            b = Binding(type="joystick", nx=nx, ny=ny, radius=0.12,
                        keys={"up": "w", "down": "s", "left": "a", "right": "d"},
                        offsets={"left": 0.07, "right": 0.07,
                                 "up": 0.12, "down": 0.12})
        elif kind == "aim":
            b = Binding(type="aim", nx=nx, ny=ny, radius=0.28, sensitivity=1.0)
        elif kind == "drag":
            b = Binding(type="tap", nx=nx, ny=ny, action="drag", hold=False,
                        start_nx=nx, start_ny=ny,
                        end_nx=min(0.98, nx + 0.15), end_ny=ny, drag_speed=1.0)
        elif kind == "multi":
            b = Binding(type="tap", nx=nx, ny=ny, action="click_multi", hold=False,
                        clicks=[{"delay": 50, "pos": {"x": nx, "y": ny}, "order": 1}])
        elif kind == "tap2":
            b = Binding(type="tap", nx=nx, ny=ny, action="click_twice", hold=False)
        else:
            b = Binding(type="tap", nx=nx, ny=ny, action="hold", hold=True)
        self.km.bindings.append(b)
        self.selected = b
        self.dirty = True
        if b.type == "tap":
            self.capturing = True
            self.say("press a key / mouse button…")
        else:
            self.say(f"{kind} placed")

    def _move_drag(self, pos, vr: pygame.Rect) -> None:
        b, part, last = self._drag_bind
        nx, ny = self._to_norm(pos, vr)
        dx, dy = nx - last[0], ny - last[1]
        self._drag_bind = (b, part, (nx, ny))
        if part == "end":
            b.end_nx, b.end_ny = _cl(b.end_nx + dx), _cl(b.end_ny + dy)
        elif isinstance(part, tuple):                      # ("click", i)
            p = b.clicks[part[1]]["pos"]
            p["x"], p["y"] = _cl(float(p["x"]) + dx), _cl(float(p["y"]) + dy)
        else:                                              # anchor
            b.nx, b.ny = _cl(b.nx + dx), _cl(b.ny + dy)
            if b.type == "tap" and b.action == "drag":
                b.start_nx, b.start_ny = b.nx, b.ny
            if b.type == "tap" and b.action == "click_multi":
                for c in b.clicks:                         # keep the macro's shape
                    c["pos"]["x"] = _cl(float(c["pos"]["x"]) + dx)
                    c["pos"]["y"] = _cl(float(c["pos"]["y"]) + dy)
        self.dirty = True

    def _hit_test(self, pos, vr: pygame.Rect):
        R = self._marker_r(vr) + 6
        for b in reversed(self.km.bindings):
            if b.type == "tap" and b.action == "drag":
                if _dist(pos, self._to_px(b.end_nx, b.end_ny, vr)) <= R:
                    return b, "end"
            if b.type == "tap" and b.action == "click_multi":
                for i in range(len(b.clicks) - 1, 0, -1):
                    p = b.clicks[i]["pos"]
                    if _dist(pos, self._to_px(float(p["x"]), float(p["y"]), vr)) <= R * 0.7:
                        return b, ("click", i)
            if _dist(pos, self._to_px(b.nx, b.ny, vr)) <= R:
                return b, "anchor"
        return None

    # -- keymap switching ---------------------------------------------------------
    KM_ROWS = 6                                   # visible keymap rows (wheel scrolls)

    def _refresh_files(self) -> None:
        now = time.monotonic()
        if now - self._files_t < 2.0 and self._files:
            return
        self._files_t = now
        try:
            files = sorted((p.name for p in self.path.parent.glob("*.json")),
                           key=str.lower)
        except OSError:
            return
        if files != self._files:
            self._files = files
            self._scroll_to_active()

    def _scroll_to_active(self) -> None:
        try:
            i = self._files.index(self.path.name)
        except ValueError:
            return
        if not (self._km_scroll <= i < self._km_scroll + self.KM_ROWS):
            self._km_scroll = max(0, min(i - self.KM_ROWS // 2,
                                         len(self._files) - self.KM_ROWS))

    def _load_keymap(self, fname: str) -> None:
        """Swap in another keymap file and apply it to the LIVE session. The
        Keymap object is mutated IN PLACE so the mirror's references stay valid."""
        from .keymap import Keymap
        p = self.path.parent / fname
        try:
            new = Keymap.load(p)
        except Exception as exc:
            self.say(f"load failed: {exc}")
            return
        self.km.name = new.name
        self.km.switch_key = new.switch_key
        self.km.bindings[:] = new.bindings
        self.path = p
        self.selected = None
        self.capturing = False
        self._drag_bind = None
        self.dirty = False
        self._scroll_to_active()
        self.say(f"applied {fname} — live!")
        if self.on_apply:
            self.on_apply()

    def _create_new(self, raw: str) -> None:
        """Make a fresh EMPTY keymap from a typed name, save it, and apply it
        live so you can immediately drag bindings onto the screen."""
        base = "".join(c for c in raw.strip() if c.isalnum() or c in " -_").strip()
        if not base:
            self.say("name was empty — cancelled")
            return
        fname = base if base.lower().endswith(".json") else base + ".json"
        p = self.path.parent / fname
        if p.exists():
            self.say(f"{fname} exists — pick another name")
            return
        # mutate the shared Keymap IN PLACE (keeps the injector's reference valid)
        self.km.name = base
        self.km.switch_key = "ctrl"
        self.km.bindings[:] = []
        self.path = p
        self.selected = None
        self.capturing = False
        self._drag_bind = None
        try:
            self.km.save(p)                       # create the file -> shows in list
        except Exception as exc:
            self.say(f"save failed: {exc}")
            return
        self.dirty = False
        self._files_t = 0.0                       # force the list to pick it up
        self._refresh_files()
        self._scroll_to_active()
        self.say(f"new keymap '{base}' — drag keys onto the screen")
        if self.on_apply:
            self.on_apply()

    # -- sidebar widgets --------------------------------------------------------
    def _sidebar_click(self, pos) -> None:
        for kind, _, _ in PALETTE:
            r = self._ui.get("pal_" + kind)
            if r and r.collidepoint(pos):
                self._drag_new = kind            # drag it onto the video
                return
        r = self._ui.get("new")
        if r and r.collidepoint(pos):
            self.naming = True
            self.name_buf = ""
            self.say("type a name for the new keymap, Enter to create")
            return
        for fname in self._files:
            r = self._ui.get("km_" + fname)
            if not r or not r.collidepoint(pos):
                continue
            if fname == self.path.name:
                self.say("already active")
                return
            now = time.monotonic()
            pf, pt = self._pending_load
            if self.dirty and (pf != fname or now - pt > 3.0):
                self._pending_load = (fname, now)
                self.say("unsaved changes — click again to discard")
                return
            self._pending_load = ("", 0.0)
            self._load_keymap(fname)
            return
        r = self._ui.get("freeze")
        if r and r.collidepoint(pos):
            self.frozen = not self.frozen
            if self.frozen:
                fr = self.get_frame()
                self.frozen_arr = None if fr is None else fr.copy()
            self.say("frame frozen — edit in peace" if self.frozen else "live video")
            return
        r = self._ui.get("switch")
        if r and r.collidepoint(pos):
            self.capturing_switch = True
            self.say("press the new switch key…")
            return
        r = self._ui.get("shot")
        if r and r.collidepoint(pos):
            self._screenshot()
            return
        r = self._ui.get("save")
        if r and r.collidepoint(pos):
            self.save()

    def _screenshot(self) -> None:
        fr = self.get_frame()
        if fr is None:
            self.say("no frame yet")
            return
        import cv2
        out = Path.home() / "Pictures" / "Wraith"
        out.mkdir(parents=True, exist_ok=True)
        p = out / time.strftime("wraith-%Y%m%d-%H%M%S.png")
        cv2.imwrite(str(p), cv2.cvtColor(fr, cv2.COLOR_RGB2BGR))
        self.say(f"saved {p.name}")

    # -- drawing ------------------------------------------------------------------
    def draw(self, screen, vr: pygame.Rect) -> None:
        self._draw_bindings(screen, vr)
        self._draw_sidebar(screen, vr)
        if self._drag_new:
            self._draw_ghost(screen)
        if self.naming:
            self._draw_name_banner(screen, vr)
        elif self.capturing or self.capturing_switch:
            self._draw_capture_banner(screen, vr)

    def _draw_name_banner(self, screen, vr: pygame.Rect) -> None:
        s = f"New keymap name:  {self.name_buf}_   (Enter = create · Esc = cancel)"
        img = self.fb.render(s, True, (255, 255, 255))
        r = img.get_rect(center=(vr.centerx, 28))
        box = r.inflate(24, 14)
        ov = pygame.Surface(box.size, pygame.SRCALPHA)
        ov.fill((40, 110, 70, 230))
        screen.blit(ov, box.topleft)
        screen.blit(img, r)

    def _marker_r(self, vr: pygame.Rect) -> int:
        return max(13, int(vr.height * 0.032))

    def _to_px(self, nx: float, ny: float, vr: pygame.Rect):
        return (vr.x + int(nx * vr.w), vr.y + int(ny * vr.h))

    def _to_norm(self, pos, vr: pygame.Rect):
        return (_cl((pos[0] - vr.x) / max(1, vr.w)),
                _cl((pos[1] - vr.y) / max(1, vr.h)))

    def _text(self, surf, s, pos, font=None, color=TEXT, center=False):
        img = (font or self.f).render(s, True, color)
        r = img.get_rect()
        if center:
            r.center = pos
        else:
            r.topleft = pos
        surf.blit(img, r)
        return r

    def _draw_bindings(self, screen, vr: pygame.Rect) -> None:
        ov = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        R = self._marker_r(vr)
        for b in self.km.bindings:
            kind = _kind(b)
            col = COLORS[kind]
            c = self._to_px(b.nx, b.ny, vr)
            sel = b is self.selected
            if kind == "joystick":
                self._draw_joystick(ov, b, c, vr, col, sel)
            elif kind == "aim":
                self._draw_aim(ov, b, c, vr, col, sel)
            elif kind == "drag":
                self._draw_dragnode(ov, b, c, vr, col, sel, R)
            elif kind == "multi":
                self._draw_multi(ov, b, c, vr, col, sel, R)
            else:
                self._draw_tap(ov, b, c, col, sel, R, double=(kind == "tap2"))
        screen.blit(ov, (0, 0))

    def _draw_tap(self, ov, b, c, col, sel, R, double=False) -> None:
        pygame.draw.circle(ov, (*col, 90), c, R)
        pygame.draw.circle(ov, (*col, 255), c, R, 2)
        if double:
            pygame.draw.circle(ov, (*col, 255), c, max(4, R - 5), 1)
        if sel:
            pygame.draw.circle(ov, (*SEL, 255), c, R + 4, 2)
        self._text(ov, b.display()[:6], c, font=self.fb, center=True)

    def _draw_multi(self, ov, b, c, vr, col, sel, R) -> None:
        pts = [self._to_px(float(n["pos"]["x"]), float(n["pos"]["y"]), vr)
               for n in b.clicks] or [c]
        if len(pts) > 1:
            pygame.draw.lines(ov, (*col, 160), False, pts, 2)
        for i, p in enumerate(pts[1:], start=2):
            pygame.draw.circle(ov, (*col, 210), p, max(7, R // 2))
            self._text(ov, str(i), p, font=self.fs, center=True)
        self._draw_tap(ov, b, c, col, sel, R)

    def _draw_dragnode(self, ov, b, c, vr, col, sel, R) -> None:
        e = self._to_px(b.end_nx, b.end_ny, vr)
        pygame.draw.line(ov, (*col, 200), c, e, 3)
        pygame.draw.circle(ov, (*col, 230), e, max(7, R // 2))
        self._text(ov, "›", e, font=self.fb, center=True)
        if sel:
            pygame.draw.circle(ov, (*SEL, 255), e, max(7, R // 2) + 3, 2)
        self._draw_tap(ov, b, c, col, sel, R)

    def _draw_joystick(self, ov, b, c, vr, col, sel) -> None:
        off = b.offsets or {}
        l = float(off.get("left", b.radius)) * vr.w
        r = float(off.get("right", b.radius)) * vr.w
        u = float(off.get("up", b.radius)) * vr.h
        d = float(off.get("down", b.radius)) * vr.h
        rect = pygame.Rect(int(c[0] - l), int(c[1] - u), int(l + r), int(u + d))
        pygame.draw.ellipse(ov, (*col, 50), rect)
        pygame.draw.ellipse(ov, (*col, 230), rect, 2)
        if sel:
            pygame.draw.ellipse(ov, (*SEL, 255), rect.inflate(8, 8), 2)
        pygame.draw.circle(ov, (*col, 255), c, 5)
        k = b.keys or {}
        self._text(ov, (k.get("up") or "w").upper(), (c[0], int(c[1] - u * 0.7)),
                   font=self.fb, center=True)
        self._text(ov, (k.get("down") or "s").upper(), (c[0], int(c[1] + d * 0.7)),
                   font=self.fb, center=True)
        self._text(ov, (k.get("left") or "a").upper(), (int(c[0] - l * 0.7), c[1]),
                   font=self.fb, center=True)
        self._text(ov, (k.get("right") or "d").upper(), (int(c[0] + r * 0.7), c[1]),
                   font=self.fb, center=True)

    def _draw_aim(self, ov, b, c, vr, col, sel) -> None:
        r = max(24, int((b.radius or 0.28) * vr.h * 0.5))
        pygame.draw.circle(ov, (*col, 35), c, r)
        pygame.draw.circle(ov, (*col, 220), c, r, 2)
        pygame.draw.line(ov, (*col, 220), (c[0] - 10, c[1]), (c[0] + 10, c[1]), 2)
        pygame.draw.line(ov, (*col, 220), (c[0], c[1] - 10), (c[0], c[1] + 10), 2)
        if sel:
            pygame.draw.circle(ov, (*SEL, 255), c, r + 4, 2)
        self._text(ov, f"AIM ×{(b.sensitivity or 1.0):.2f}", (c[0], c[1] - r - 12),
                   font=self.fb, center=True)

    def _draw_ghost(self, screen) -> None:
        col = COLORS[self._drag_new]
        pygame.draw.circle(screen, col, self._mouse, 16, 3)
        self._text(screen, self._drag_new.upper(),
                   (self._mouse[0], self._mouse[1] - 26), font=self.fs, center=True)

    def _draw_capture_banner(self, screen, vr: pygame.Rect) -> None:
        s = "press the new SWITCH key  (ESC cancels)" if self.capturing_switch \
            else "press a key or mouse button to bind  (ESC cancels)"
        img = self.fb.render(s, True, (255, 255, 255))
        r = img.get_rect(center=(vr.centerx, 28))
        box = r.inflate(24, 14)
        ov = pygame.Surface(box.size, pygame.SRCALPHA)
        ov.fill((180, 60, 60, 220))
        screen.blit(ov, box.topleft)
        screen.blit(img, r)

    def _draw_sidebar(self, screen, vr: pygame.Rect) -> None:
        sw, sh = screen.get_size()
        x0 = sw - self.SIDEBAR_W       # window edge, not vr.right (letterboxing)
        w = sw - x0
        pygame.draw.rect(screen, SIDE_BG, (x0, 0, w, sh))
        pygame.draw.line(screen, SIDE_EDGE, (x0, 0), (x0, sh))
        pad = 10
        x = x0 + pad
        iw = w - 2 * pad               # inner content width
        y = 8
        bottom = sh - 6

        # Everything flows STRICTLY top->bottom (no bottom-anchoring) so it can
        # never overlap. When the window is short, palette/buttons shrink and the
        # lower reference sections simply stop drawing once we run out of height.
        compact = sh < 720             # short window -> tighter layout

        # -- header -----------------------------------------------------------
        self._text(screen, "KEYMAP EDITOR", (x, y), font=self.fb, color=(138, 180, 248))
        self._text(screen, self.path.name + (" *" if self.dirty else ""),
                   (x, y + 16), font=self.fs, color=DIM)
        y += 34

        # -- palette ----------------------------------------------------------
        pal_h = 28 if compact else 36
        for kind, label, hint in PALETTE:
            r = pygame.Rect(x, y, iw, pal_h)
            self._ui["pal_" + kind] = r
            pygame.draw.rect(screen, ITEM_BG, r, border_radius=6)
            pygame.draw.rect(screen, COLORS[kind], (r.x, r.y, 5, r.h), border_radius=3)
            if compact:                # one line: label + faint hint after it
                self._text(screen, label, (r.x + 12, r.y + (pal_h - 14) // 2), font=self.fb)
            else:
                self._text(screen, label, (r.x + 12, r.y + 3), font=self.fb)
                self._text(screen, hint, (r.x + 12, r.y + 19), font=self.fs, color=DIM)
            y += pal_h + 3
        y += 4

        # -- action buttons ---------------------------------------------------
        btn_h = 24 if compact else 28
        for bid, label, col in (
            ("freeze", "UNFREEZE — GO LIVE" if self.frozen else "FREEZE FRAME", ITEM_BG),
            ("switch", f"SWITCH KEY: {(self.km.switch_key or 'ctrl').upper()}", ITEM_BG),
            ("shot", "SCREENSHOT → PNG", ITEM_BG),
        ):
            r = pygame.Rect(x, y, iw, btn_h)
            self._ui[bid] = r
            pygame.draw.rect(screen, col, r, border_radius=6)
            if bid == "freeze" and self.frozen:
                pygame.draw.rect(screen, (138, 180, 248), r, 2, border_radius=6)
            self._text(screen, label, r.center, font=self.fb, center=True)
            y += btn_h + 4
        # NEW (blank keymap) + SAVE share a row
        half = (iw - 6) // 2
        rn = pygame.Rect(x, y, half, btn_h)
        rs = pygame.Rect(x + half + 6, y, iw - half - 6, btn_h)
        self._ui["new"] = rn
        self._ui["save"] = rs
        pygame.draw.rect(screen, (46, 96, 148), rn, border_radius=6)
        self._text(screen, "+ NEW", rn.center, font=self.fb, center=True)
        pygame.draw.rect(screen, GREEN, rs, border_radius=6)
        self._text(screen, "SAVE *" if self.dirty else "SAVE", rs.center, font=self.fb, center=True)
        y += btn_h + 4

        # -- live status message (right under the buttons, always visible) ----
        if self._msg and time.monotonic() - self._msg_t < 4.0:
            self._text(screen, self._msg[:30], (x, y), font=self.fs, color=OK)
        y += 16

        # -- selected-binding info (compact, only when something is picked) ---
        if self.selected is not None:
            b = self.selected
            kind = _kind(b)
            self._text(screen, f"{kind.upper()} · {b.display()}", (x, y),
                       font=self.fb, color=COLORS[kind])
            tune = {"tap": f"hold {'ON' if b.hold else 'OFF'} (H)",
                    "drag": f"speed x{b.drag_speed:.2f} (wheel)",
                    "multi": f"{len(b.clicks)} pts · R-click adds",
                    "joystick": f"size {b.radius:.3f} (wheel)",
                    "aim": f"sens x{(b.sensitivity or 1):.2f} (wheel)"}.get(kind, "")
            self._text(screen, tune, (x, y + 15), font=self.fs, color=DIM)
            y += 34

        # -- keymap list (click to apply) — fills remaining, rows that fit ----
        self._refresh_files()
        for k in [k for k in self._ui if k.startswith("km_")]:
            del self._ui[k]                       # drop rects of deleted files
        n = len(self._files)
        # reserve a little space for at least a couple of hint lines if possible
        hint_reserve = 0 if (bottom - y) < 90 else 46
        rows_fit = max(1, (bottom - y - 16 - hint_reserve) // 17)
        vis = max(1, min(self.KM_ROWS, rows_fit, n))
        self._km_visible = vis
        self._km_scroll = max(0, min(self._km_scroll, max(0, n - vis)))
        s = self._km_scroll
        label = "KEYMAPS — click to apply"
        if n > vis:
            label += f"  {s + 1}-{min(s + vis, n)}/{n}"
        self._text(screen, label, (x, y), font=self.fs, color=DIM)
        y += 15
        for fname in self._files[s:s + vis]:
            r = pygame.Rect(x, y, iw, 16)
            self._ui["km_" + fname] = r
            active = fname == self.path.name
            if active:
                pygame.draw.rect(screen, (32, 48, 38), r, border_radius=4)
            self._text(screen, ("● " if active else "· ") + fname[:24],
                       (r.x + 3, r.y + 1), font=self.fs,
                       color=OK if active else TEXT)
            y += 17
        y += 6

        # -- hints — only the lines that still fit above the window bottom ----
        for h in ("+NEW = blank keymap · click a name to load",
                  "drag a type onto the video", "click=select · drag=move",
                  "dbl-click=rebind · wheel=size", "right-click=add MULTI point",
                  "H=hold · DEL=delete", "Ctrl+S=save · F10=play"):
            if y > bottom - 13:
                break
            self._text(screen, h, (x, y), font=self.fs, color=DIM)
            y += 13
