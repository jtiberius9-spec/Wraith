"""
wraith.keymap — keymap data model + JSON persistence.

All positions are NORMALIZED (0.0..1.0) over the LANDSCAPE screen, so a keymap
stays valid regardless of device resolution or window size. The drag-and-drop
editor reads/writes exactly this structure.

Binding types
-------------
- "tap"      : key/mouse button -> single tap (optionally held while pressed)
- "joystick" : WASD-style movement -> a held touch dragged around a center
- "aim"      : mouse movement -> camera look (relative drag + auto-recenter)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Binding:
    type: str                       # "tap" | "joystick" | "aim"
    nx: float                       # normalized x of the anchor point
    ny: float                       # normalized y of the anchor point
    key: str = ""                   # for tap/aim: "f", "space", "mouse_left", ...
    label: str = ""                 # display label in editor
    hold: bool = True               # tap: keep finger down while key is held
    # joystick:
    keys: dict = field(default_factory=dict)   # {"up","down","left","right"}
    radius: float = 0.10            # joystick throw / aim look-area radius (norm)
    # aim:
    sensitivity: float = 1.0        # mouse-look multiplier
    # QtScrcpy-compatible advanced nodes:
    action: str = "hold"            # hold | click_twice | click_multi | drag
    clicks: list = field(default_factory=list)
    start_nx: float = 0.0
    start_ny: float = 0.0
    end_nx: float = 0.0
    end_ny: float = 0.0
    drag_speed: float = 1.0
    start_delay: int = 0
    offsets: dict = field(default_factory=dict)

    def display(self) -> str:
        if self.label:
            return self.label
        if self.type == "joystick":
            return "WASD"
        if self.type == "aim":
            return "AIM"
        return self.key.replace("mouse_", "M:").upper() or "?"


@dataclass
class Keymap:
    name: str = "Untitled"
    bindings: list[Binding] = field(default_factory=list)
    switch_key: str = "ctrl"        # key that toggles keymap <-> free mouse

    # -- persistence ----------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Keymap":
        data = json.loads(Path(path).read_text())
        if "keyMapNodes" in data or "mouseMoveMap" in data:
            return cls.from_qtscrcpy(data, Path(path).stem)
        bindings = [Binding(**b) for b in data.get("bindings", [])]
        return cls(name=data.get("name", "Untitled"), bindings=bindings,
                   switch_key=data.get("switch_key", "ctrl"))

    @classmethod
    def from_qtscrcpy(cls, data: dict, name: str = "Imported") -> "Keymap":
        def key_name(raw: str) -> str:
            mapping = {
                "LeftButton": "mouse_left",
                "RightButton": "mouse_right",
                "MiddleButton": "mouse_middle",
                "Key_Control": "ctrl",
                "Key_Shift": "shift",
                "Key_Alt": "alt",
                "Key_Space": "space",
                "Key_Tab": "tab",
                "Key_Escape": "esc",
                "Key_Return": "enter",
                "Key_Enter": "enter",
            }
            if raw in mapping:
                return mapping[raw]
            if raw.startswith("Key_"):
                return raw[4:].lower()
            return raw.lower()

        switch = key_name(data.get("switchKey", "Key_Control")) or "ctrl"
        bindings: list[Binding] = []
        aim = data.get("mouseMoveMap", {})
        if aim.get("type") == "KMT_MOUSE_MOVE":
            pos = aim.get("startPos", {})
            bindings.append(Binding(
                type="aim",
                nx=float(pos.get("x", 0.6)),
                ny=float(pos.get("y", 0.5)),
                radius=0.28,
                sensitivity=float(aim.get("speedRatioX", 1.0) or 1.0),
            ))
        for node in data.get("keyMapNodes", []):
            typ = node.get("type")
            if typ == "KMT_CLICK":
                pos = node.get("pos", {})
                bindings.append(Binding(
                    type="tap",
                    nx=float(pos.get("x", 0.5)),
                    ny=float(pos.get("y", 0.5)),
                    key=key_name(node.get("key", "")),
                ))
            elif typ == "KMT_CLICK_TWICE":
                pos = node.get("pos", {})
                bindings.append(Binding(
                    type="tap",
                    nx=float(pos.get("x", 0.5)),
                    ny=float(pos.get("y", 0.5)),
                    key=key_name(node.get("key", "")),
                    hold=False,
                    action="click_twice",
                ))
            elif typ == "KMT_CLICK_MULTI":
                clicks = node.get("clickNodes") or []
                if clicks:
                    pos = clicks[0].get("pos", {})
                    bindings.append(Binding(
                        type="tap",
                        nx=float(pos.get("x", 0.5)),
                        ny=float(pos.get("y", 0.5)),
                        key=key_name(node.get("key", "")),
                        hold=False,
                        action="click_multi",
                        clicks=clicks,
                    ))
            elif typ == "KMT_DRAG":
                start = node.get("startPos", {})
                end = node.get("endPos", {})
                bindings.append(Binding(
                    type="tap",
                    nx=float(start.get("x", 0.5)),
                    ny=float(start.get("y", 0.5)),
                    key=key_name(node.get("key", "")),
                    hold=False,
                    action="drag",
                    start_nx=float(start.get("x", 0.5)),
                    start_ny=float(start.get("y", 0.5)),
                    end_nx=float(end.get("x", 0.5)),
                    end_ny=float(end.get("y", 0.5)),
                    drag_speed=float(node.get("dragSpeed", 1.0) or 1.0),
                    start_delay=int(node.get("startDelay", 0) or 0),
                ))
            elif typ == "KMT_STEER_WHEEL":
                pos = node.get("centerPos", {})
                radius = max(
                    float(node.get("leftOffset", 0.1) or 0.1),
                    float(node.get("rightOffset", 0.1) or 0.1),
                    float(node.get("upOffset", 0.1) or 0.1),
                    float(node.get("downOffset", 0.1) or 0.1),
                )
                bindings.append(Binding(
                    type="joystick",
                    nx=float(pos.get("x", 0.18)),
                    ny=float(pos.get("y", 0.72)),
                    radius=radius,
                    offsets={
                        "left": float(node.get("leftOffset", radius) or radius),
                        "right": float(node.get("rightOffset", radius) or radius),
                        "up": float(node.get("upOffset", radius) or radius),
                        "down": float(node.get("downOffset", radius) or radius),
                    },
                    keys={
                        "up": key_name(node.get("upKey", "Key_W")),
                        "down": key_name(node.get("downKey", "Key_S")),
                        "left": key_name(node.get("leftKey", "Key_A")),
                        "right": key_name(node.get("rightKey", "Key_D")),
                    },
                ))
        return cls(name=name, bindings=bindings, switch_key=switch)

    def save(self, path: str | Path) -> None:
        data = {"name": self.name, "switch_key": self.switch_key,
                "bindings": [asdict(b) for b in self.bindings]}
        Path(path).write_text(json.dumps(data, indent=2))

    # -- helpers --------------------------------------------------------------
    def aim_binding(self) -> Binding | None:
        return next((b for b in self.bindings if b.type == "aim"), None)

    def joystick(self) -> Binding | None:
        return next((b for b in self.bindings if b.type == "joystick"), None)

    def taps(self) -> list[Binding]:
        return [b for b in self.bindings if b.type == "tap"]
