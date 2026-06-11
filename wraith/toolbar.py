"""
wraith.toolbar — floating nav sidebar over the mirror (QtScrcpy-style).

A vertical strip of device controls drawn on the LEFT edge of the video in
PORTRAIT mouse mode only. It vanishes the moment the phone goes landscape
(i.e. you launch a game), so it never sits on top of gameplay.

Buttons send scrcpy control messages through the existing control socket:
power/volume/back/home/recents = INJECT_KEYCODE; screen on/off = SET_DISPLAY_POWER;
notifications = EXPAND_NOTIFICATION_PANEL.
"""

from __future__ import annotations

import pygame

# Android keycodes
KC_HOME = 3
KC_BACK = 4
KC_VOL_UP = 24
KC_VOL_DOWN = 25
KC_POWER = 26
KC_APP_SWITCH = 187

BAR_W = 48
PAD = 7
BTN = 38                       # button box size
BG = (22, 24, 28, 235)
EDGE = (70, 74, 84)
BTN_BG = (42, 46, 54)
BTN_HOVER = (60, 66, 80)
ICON = (224, 228, 234)
ACCENT = (138, 180, 248)
WARN = (239, 120, 90)


class Toolbar:
    def __init__(self, *, send_key, set_screen, expand_notif):
        self.send_key = send_key       # fn(keycode): inject down+up
        self.set_screen = set_screen   # fn(bool): SET_DISPLAY_POWER
        self.expand_notif = expand_notif  # fn(): EXPAND_NOTIFICATION_PANEL
        self.collapsed = False
        self._rects: dict[str, pygame.Rect] = {}
        self._hover: str | None = None
        self._flash: tuple[str, int] | None = None     # (id, frames) press feedback

        # (id, glyph, action) — order top→bottom
        self.items = [
            ("power",   self._g_power,   lambda: self.send_key(KC_POWER)),
            ("volup",   self._g_volup,   lambda: self.send_key(KC_VOL_UP)),
            ("voldown", self._g_voldown, lambda: self.send_key(KC_VOL_DOWN)),
            ("scr_on",  self._g_eye,     lambda: self.set_screen(True)),
            ("scr_off", self._g_eyeoff,  lambda: self.set_screen(False)),
            ("notif",   self._g_bell,    lambda: self.expand_notif()),
            ("recents", self._g_square,  lambda: self.send_key(KC_APP_SWITCH)),
            ("home",    self._g_home,    lambda: self.send_key(KC_HOME)),
            ("back",    self._g_back,    lambda: self.send_key(KC_BACK)),
        ]

    # -- state ----------------------------------------------------------------
    def visible(self, fw, fh, game_mode, edit_mode) -> bool:
        # portrait only; hidden in landscape (games), edit, or aim/game mode
        return (fh > fw) and not game_mode and not edit_mode

    def set_hover(self, pos) -> None:
        self._hover = None
        for bid, r in self._rects.items():
            if r.collidepoint(pos):
                self._hover = bid
                return

    def handle_click(self, pos) -> bool:
        """Return True if the click hit the toolbar (and was consumed)."""
        tgl = self._rects.get("toggle")
        if tgl and tgl.collidepoint(pos):
            self.collapsed = not self.collapsed
            return True
        if self.collapsed:
            return False
        for bid, action in [(i[0], i[2]) for i in self.items]:
            r = self._rects.get(bid)
            if r and r.collidepoint(pos):
                try:
                    action()
                except Exception:
                    pass
                self._flash = (bid, 6)
                return True
        # swallow clicks anywhere on the bar so they don't tap the phone
        bar = self._rects.get("bar")
        return bool(bar and bar.collidepoint(pos))

    # -- drawing --------------------------------------------------------------
    def draw(self, screen, video_rect: pygame.Rect) -> None:
        self._rects.clear()
        x = video_rect.x
        if self.collapsed:
            r = pygame.Rect(x, video_rect.y + 8, 16, 46)
            self._rects["toggle"] = r
            ov = pygame.Surface(r.size, pygame.SRCALPHA)
            ov.fill(BG)
            screen.blit(ov, r.topleft)
            self._chevron(screen, r.center, right=True)
            return

        n = len(self.items)
        bar_h = PAD + (n + 1) * (BTN + PAD)        # +1 for the toggle row
        bar_h = min(bar_h, video_rect.h)
        bar = pygame.Rect(x, video_rect.y + max(0, (video_rect.h - bar_h) // 2),
                          BAR_W, bar_h)
        self._rects["bar"] = bar
        ov = pygame.Surface(bar.size, pygame.SRCALPHA)
        ov.fill(BG)
        screen.blit(ov, bar.topleft)
        pygame.draw.line(screen, EDGE, (bar.right, bar.y), (bar.right, bar.bottom))

        cx = bar.x + BAR_W // 2
        y = bar.y + PAD
        # collapse toggle
        tr = pygame.Rect(cx - BTN // 2, y, BTN, BTN)
        self._rects["toggle"] = tr
        self._chevron(screen, tr.center, right=False)
        y += BTN + PAD
        for bid, glyph, _ in self.items:
            r = pygame.Rect(cx - BTN // 2, y, BTN, BTN)
            self._rects[bid] = r
            flashing = self._flash and self._flash[0] == bid
            bgc = ACCENT if flashing else (BTN_HOVER if self._hover == bid else BTN_BG)
            pygame.draw.rect(screen, bgc, r, border_radius=8)
            col = (20, 22, 26) if flashing else ICON
            glyph(screen, r.centerx, r.centery, col)
            y += BTN + PAD
        if self._flash:
            bid, f = self._flash
            self._flash = (bid, f - 1) if f > 1 else None

    # -- glyphs (drawn vector icons) ------------------------------------------
    def _chevron(self, s, center, right):
        cx, cy = center
        d = 5
        pts = ([(cx - d, cy - d), (cx + d, cy), (cx - d, cy + d)] if right
               else [(cx + d, cy - d), (cx - d, cy), (cx + d, cy + d)])
        pygame.draw.lines(s, ICON, False, pts, 2)

    def _g_power(self, s, cx, cy, col):
        pygame.draw.arc(s, col, (cx - 9, cy - 8, 18, 18), 0.6, 2.54, 2)
        pygame.draw.arc(s, col, (cx - 9, cy - 8, 18, 18), 3.74, 5.68, 2)
        pygame.draw.line(s, col, (cx, cy - 11), (cx, cy - 1), 2)

    def _g_volup(self, s, cx, cy, col):
        self._speaker(s, cx - 3, cy, col)
        pygame.draw.arc(s, col, (cx + 1, cy - 7, 12, 14), -0.8, 0.8, 2)
        pygame.draw.line(s, col, (cx + 9, cy - 4), (cx + 9, cy + 4), 2)  # plus mid handled below
        pygame.draw.line(s, col, (cx + 6, cy), (cx + 12, cy), 2)

    def _g_voldown(self, s, cx, cy, col):
        self._speaker(s, cx - 3, cy, col)
        pygame.draw.line(s, col, (cx + 6, cy), (cx + 12, cy), 2)

    def _speaker(self, s, cx, cy, col):
        pygame.draw.polygon(s, col, [(cx - 6, cy - 3), (cx - 2, cy - 3),
                                     (cx + 2, cy - 7), (cx + 2, cy + 7),
                                     (cx - 2, cy + 3), (cx - 6, cy + 3)], 0)

    def _g_eye(self, s, cx, cy, col):
        pygame.draw.ellipse(s, col, (cx - 11, cy - 6, 22, 12), 2)
        pygame.draw.circle(s, col, (cx, cy), 3)

    def _g_eyeoff(self, s, cx, cy, col):
        pygame.draw.ellipse(s, col, (cx - 11, cy - 6, 22, 12), 2)
        pygame.draw.circle(s, col, (cx, cy), 3)
        pygame.draw.line(s, WARN, (cx - 11, cy + 9), (cx + 11, cy - 9), 2)

    def _g_bell(self, s, cx, cy, col):
        pygame.draw.arc(s, col, (cx - 7, cy - 9, 14, 16), 0.0, 3.14, 2)
        pygame.draw.line(s, col, (cx - 7, cy + 4), (cx + 7, cy + 4), 2)
        pygame.draw.circle(s, col, (cx, cy + 8), 2)

    def _g_square(self, s, cx, cy, col):
        pygame.draw.rect(s, col, (cx - 8, cy - 8, 16, 16), 2, border_radius=2)

    def _g_home(self, s, cx, cy, col):
        pygame.draw.circle(s, col, (cx, cy), 9, 2)

    def _g_back(self, s, cx, cy, col):
        pts = [(cx + 5, cy - 8), (cx - 6, cy), (cx + 5, cy + 8)]
        pygame.draw.lines(s, col, False, pts, 2)
