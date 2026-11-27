"""Keyboard backlight backends.

Three ways to reach the keyboard, tried in this order:

``ite829x``  Per-key RGB keyboards driven by the ITE 829x USB controller
             (USB id 048d:8910 - the only keyboard controller id in the
             Windows app).  Talked to directly over hidraw, with all the
             controller's built-in effects.
``leds``     Any kernel driver that exposes the keyboard as an LED class
             device (upstream ``uniwill-laptop``, ``tuxedo-drivers`` ...).
``ec``       Single-zone RGB / white keyboards controlled by EC registers
             (the Windows app's "SingleZone" keyboard).

Effects not built into the hardware are animated in software by the daemon.
"""

from __future__ import annotations

import fcntl
import os
import re

from . import colors as col
from . import constants as C
from . import sysfs
from .colors import RGB

EFFECTS = {
    "static": "Static",
    "breathing": "Breathing",
    "flash": "Flash",
    "wave": "Wave",
    "rainbow_wave": "Rainbow wave",
    "rainbow": "Spectrum cycle",
    "random": "Random",
    "scan": "Scan",
    "snake": "Snake",
    "off": "Off",
}
#: Effects the daemon can animate in software on any RGB keyboard.
SOFTWARE_EFFECTS = {"breathing", "flash", "rainbow", "wave", "rainbow_wave"}
DIRECTIONS = ["left", "right", "up", "down", "up_left", "up_right", "down_left", "down_right"]


class KeyboardError(RuntimeError):
    pass


class Keyboard:
    """Common interface.  Brightness is always a float 0..1 here."""

    backend = "none"
    kind = "none"  # white | rgb | multizone | perkey
    zones = 1
    hw_effects: frozenset[str] = frozenset()
    #: rough cost of one full colour update - limits software animation fps
    frame_interval = 0.1

    def set_colors(self, colors: list[RGB], brightness: float) -> None:
        raise NotImplementedError

    def set_brightness(self, brightness: float) -> None:
        raise NotImplementedError

    def set_hw_effect(self, effect: str, colors: list[RGB], brightness: float,
                      speed: int, direction: str) -> None:
        raise KeyboardError(f"{self.backend} keyboard has no hardware effect {effect!r}")

    def off(self) -> None:
        self.set_brightness(0.0)

    @property
    def is_rgb(self) -> bool:
        return self.kind != "white"

    def effects(self) -> list[str]:
        if not self.is_rgb:
            return ["static", "breathing", "off"]
        names = {"static", "off"} | set(self.hw_effects) | SOFTWARE_EFFECTS
        return [e for e in EFFECTS if e in names]

    def info(self) -> dict:
        return {
            "backend": self.backend,
            "kind": self.kind,
            "zones": self.zones,
            "rgb": self.is_rgb,
            "effects": self.effects(),
            "hw_effects": sorted(self.hw_effects),
        }


def spread(colors: list[RGB], n: int) -> list[RGB]:
    """Stretch a list of zone colours across ``n`` LEDs/zones."""
    if not colors:
        colors = [(255, 255, 255)]
    if len(colors) == 1 or n == 1:
        return [colors[0]] * n
    return [colors[min(len(colors) - 1, i * len(colors) // n)] for i in range(n)]


# ---------------------------------------------------------------------------
# EC single-zone keyboard
# ---------------------------------------------------------------------------
class ECKeyboard(Keyboard):
    backend = "ec"
    frame_interval = 0.06  # 5 EC writes x 6 ms + margin

    def __init__(self, ec, levels: int = 4) -> None:
        self.ec = ec
        self.levels = max(1, levels)
        status = ec.read(C.KBD_STATUS)
        self.kind = "white" if status & C.KBD_WHITE_ONLY else "rgb"

    @staticmethod
    def probe(ec) -> bool:
        try:
            support2 = ec.read(C.SUPPORT_2)
            status = ec.read(C.KBD_STATUS)
        except Exception:
            return False
        return bool(support2 & 0x04) or bool(status & C.KBD_WHITE_ONLY) or bool(status & C.KBD_BRIGHTNESS_MASK)

    def _level(self, brightness: float) -> int:
        return max(0, min(self.levels, round(brightness * self.levels)))

    def set_brightness(self, brightness: float) -> None:
        level = self._level(brightness)
        mask = C.KBD_BRIGHTNESS_MASK | C.KBD_APPLY | C.KBD_POWER_OFF
        self.ec.update_bits(C.KBD_STATUS, mask, (level << C.KBD_BRIGHTNESS_SHIFT) | C.KBD_APPLY)

    def write_rgb(self, rgb: RGB) -> None:
        vals = [round(c * C.RGB_MAX / 255) for c in rgb]
        if not any(vals):
            vals = [1, 1, 1]  # 0,0,0 makes the EC restore its default colour
        for reg, v in zip((C.RGB_RED, C.RGB_GREEN, C.RGB_BLUE), vals):
            self.ec.write(reg, v)
        self.ec.update_bits(C.TRIGGER, C.RGB_APPLY_COLOR, C.RGB_APPLY_COLOR)

    def set_colors(self, colors: list[RGB], brightness: float) -> None:
        if self.is_rgb:
            self.write_rgb(colors[0] if colors else (255, 255, 255))
        self.set_brightness(brightness)


# ---------------------------------------------------------------------------
# kernel LED class devices
# ---------------------------------------------------------------------------
def _natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


class LedClassKeyboard(Keyboard):
    backend = "leds"

    def __init__(self, led_dirs: list[str]) -> None:
        if not led_dirs:
            raise KeyboardError("no keyboard LED devices")
        self.leds = led_dirs
        first = led_dirs[0]
        self.max_brightness = sysfs.read_int(os.path.join(first, "max_brightness"), 1) or 1
        self.multicolor = sysfs.exists(os.path.join(first, "multi_intensity"))
        self.zones = len(led_dirs)
        if not self.multicolor:
            self.kind = "white"
        elif self.zones == 1:
            self.kind = "rgb"
        elif self.zones <= 8:
            self.kind = "multizone"
        else:
            self.kind = "perkey"
        self.frame_interval = 0.05 if self.zones <= 8 else 0.15
        self._order = self._channel_order(first)
        self._max_int = self._max_intensity(first)

    @staticmethod
    def discover() -> list[str]:
        leds = [d for d in sysfs.g("class/leds/*") if "kbd_backlight" in os.path.basename(d)]
        return sorted(leds, key=lambda d: _natural_key(os.path.basename(d)))

    @staticmethod
    def _channel_order(d: str) -> list[int]:
        idx = (sysfs.read(os.path.join(d, "multi_index")) or "red green blue").split()
        pos = {"red": 0, "green": 1, "blue": 2}
        return [pos.get(name, i) for i, name in enumerate(idx)][:3] or [0, 1, 2]

    @staticmethod
    def _max_intensity(d: str) -> int:
        mmi = sysfs.read(os.path.join(d, "multi_max_intensity"))
        if mmi:
            try:
                return max(int(x) for x in mmi.split())
            except ValueError:
                pass
        # upstream uniwill-laptop caps each channel at 50
        return C.RGB_MAX if os.path.basename(d).startswith("uniwill") else 255

    def _bri(self, brightness: float) -> int:
        b = round(brightness * self.max_brightness)
        if brightness > 0 and b == 0:
            b = 1
        return max(0, min(self.max_brightness, b))

    def set_brightness(self, brightness: float) -> None:
        b = self._bri(brightness)
        for d in self.leds:
            sysfs.write(os.path.join(d, "brightness"), b)

    def set_colors(self, colors: list[RGB], brightness: float) -> None:
        b = self._bri(brightness)
        per_led = spread(colors, self.zones)
        for d, rgb in zip(self.leds, per_led):
            if self.multicolor:
                vals = [round(rgb[i] * self._max_int / 255) for i in self._order]
                sysfs.write(os.path.join(d, "multi_intensity"), " ".join(map(str, vals)))
            sysfs.write(os.path.join(d, "brightness"), b)


# ---------------------------------------------------------------------------
# ITE 829x (USB 048d:8910) per-key keyboard over hidraw
# ---------------------------------------------------------------------------
ITE829X_VID, ITE829X_PID = 0x048D, 0x8910


def _hidiocsfeature(length: int) -> int:
    # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x06


class HidrawTransport:
    def __init__(self, devnode: str) -> None:
        self.devnode = devnode
        self.fd = os.open(devnode, os.O_RDWR | os.O_NONBLOCK)

    def send_feature(self, data: bytes) -> None:
        buf = bytearray(data)
        fcntl.ioctl(self.fd, _hidiocsfeature(len(buf)), buf, True)

    def close(self) -> None:
        os.close(self.fd)


def find_ite829x() -> str | None:
    """Return /dev/hidrawN of the ITE 829x interface that owns report id 0xCC."""
    want = f"HID_ID=0003:{ITE829X_VID:08X}:{ITE829X_PID:08X}"
    candidates = []
    for d in sysfs.g("class/hidraw/hidraw*"):
        uevent = sysfs.read(os.path.join(d, "device/uevent")) or ""
        if want not in uevent.upper():
            continue
        node = "/dev/" + os.path.basename(d)
        try:
            with open(os.path.join(d, "device/report_descriptor"), "rb") as fh:
                desc = fh.read()
        except OSError:
            desc = b""
        if b"\x85\xcc" in desc:
            return node
        candidates.append(node)
    return candidates[0] if candidates else None


class Ite829xKeyboard(Keyboard):
    backend = "ite829x"
    kind = "perkey"
    ROWS, COLS = 6, 20
    zones = ROWS * COLS
    BRIGHTNESS_MAX = 10
    frame_interval = 0.12
    hw_effects = frozenset({"breathing", "flash", "wave", "rainbow_wave", "rainbow", "random", "scan", "snake"})

    # protocol: 6-byte feature report [0xCC, cmd, d0, d1, d2, d3]
    CMD_ANIM, CMD_SET_LED, CMD_BRIGHTNESS = 0x00, 0x01, 0x09
    CMD_BREATH, CMD_FLASH = 0x0A, 0x0B
    CMD_WAVE_COLOR, CMD_SNAKE_COLOR, CMD_SCAN_COLOR, CMD_RANDOM_COLOR = 0x15, 0x16, 0x17, 0x18
    ANIM_RAINBOW, ANIM_WAVE, ANIM_RANDOM, ANIM_SCAN, ANIM_SNAKE, ANIM_CLEAR = 0x02, 0x04, 0x09, 0x0A, 0x0B, 0x0C
    CUSTOM, SLOT_CUSTOM, SLOT_PRESET = 0xAA, 0xA1, 0x71
    WAVE_DIR = {"up_left": 0, "up_right": 1, "down_left": 2, "down_right": 3,
                "up": 4, "down": 5, "left": 6, "right": 7}
    SNAKE_DIR = {"up_left": 0, "up_right": 1, "down_left": 2, "down_right": 3}

    def __init__(self, transport) -> None:
        self.t = transport
        self._speed = 5

    @staticmethod
    def led_id(row: int, col_: int) -> int:
        return ((row & 0x07) << 5) | (col_ & 0x1F)

    def cmd(self, c: int, d0: int = 0, d1: int = 0, d2: int = 0, d3: int = 0) -> None:
        self.t.send_feature(bytes([0xCC, c, d0, d1, d2, d3]))

    def _level(self, brightness: float) -> int:
        b = round(brightness * self.BRIGHTNESS_MAX)
        if brightness > 0 and b == 0:
            b = 1
        return max(0, min(self.BRIGHTNESS_MAX, b))

    def set_brightness(self, brightness: float, speed: int | None = None) -> None:
        if speed is not None:
            self._speed = max(0, min(10, speed))
        self.cmd(self.CMD_BRIGHTNESS, self._level(brightness), self._speed)

    def off(self) -> None:
        self.cmd(self.CMD_BRIGHTNESS, 0, 0)

    def set_colors(self, colors: list[RGB], brightness: float) -> None:
        self.cmd(self.CMD_ANIM, self.ANIM_CLEAR)
        self.set_brightness(brightness)
        if len(colors) <= 1 or len(colors) == self.zones:
            per_key = spread(colors, self.zones)
        else:  # N zones laid out left-to-right, like the 4-zone keyboards
            cols = spread(colors, self.COLS)
            per_key = [cols[c] for _r in range(self.ROWS) for c in range(self.COLS)]
        for r in range(self.ROWS):
            for c in range(self.COLS):
                rgb = per_key[r * self.COLS + c]
                self.cmd(self.CMD_SET_LED, self.led_id(r, c), *rgb)

    def set_hw_effect(self, effect: str, colors: list[RGB], brightness: float,
                      speed: int, direction: str) -> None:
        custom = colors[0] if colors else None
        if effect == "breathing":
            self.cmd(self.CMD_BREATH, self.CUSTOM, *custom) if custom else self.cmd(self.CMD_BREATH)
        elif effect == "flash":
            self.cmd(self.CMD_FLASH, self.CUSTOM, *custom) if custom else self.cmd(self.CMD_FLASH)
        elif effect in ("wave", "rainbow_wave"):
            self.cmd(self.CMD_ANIM, self.ANIM_WAVE)
            idx = self.WAVE_DIR.get(direction, self.WAVE_DIR["right"])
            if effect == "wave" and custom:
                self.cmd(self.CMD_WAVE_COLOR, self.SLOT_CUSTOM + idx, *custom)
            else:
                self.cmd(self.CMD_WAVE_COLOR, self.SLOT_PRESET + idx)
        elif effect == "rainbow":
            self.cmd(self.CMD_ANIM, self.ANIM_RAINBOW)
        elif effect == "random":
            self.cmd(self.CMD_ANIM, self.ANIM_RANDOM)
            if custom and len(colors) == 1:
                self.cmd(self.CMD_RANDOM_COLOR, self.SLOT_CUSTOM, *custom)
        elif effect == "scan":
            self.cmd(self.CMD_ANIM, self.ANIM_SCAN)
            for i, rgb in enumerate((colors or [(255, 0, 0), (0, 0, 255)])[:2]):
                self.cmd(self.CMD_SCAN_COLOR, self.SLOT_CUSTOM + i, *rgb)
        elif effect == "snake":
            self.cmd(self.CMD_ANIM, self.ANIM_SNAKE)
            idx = self.SNAKE_DIR.get(direction, 0)
            if custom:
                self.cmd(self.CMD_SNAKE_COLOR, self.SLOT_CUSTOM + idx, *custom)
            else:
                self.cmd(self.CMD_SNAKE_COLOR, self.SLOT_PRESET + idx)
        else:
            raise KeyboardError(f"ITE 829x has no effect {effect!r}")
        self.set_brightness(brightness, speed)


# ---------------------------------------------------------------------------
def open_keyboard(ec=None, prefer: str = "auto", ec_levels: int = 4) -> Keyboard | None:
    order = ["ite829x", "leds", "ec"] if prefer == "auto" else [prefer]
    for kind in order:
        try:
            if kind == "ite829x":
                node = find_ite829x()
                if node:
                    return Ite829xKeyboard(HidrawTransport(node))
            elif kind == "leds":
                leds = LedClassKeyboard.discover()
                if leds:
                    return LedClassKeyboard(leds)
            elif kind == "ec" and ec is not None and ECKeyboard.probe(ec):
                return ECKeyboard(ec, ec_levels)
        except (OSError, KeyboardError):
            continue
    return None


def describe_effect_support(kb: Keyboard, effect: str) -> str:
    if effect in kb.hw_effects:
        return "hardware"
    if effect in SOFTWARE_EFFECTS and kb.is_rgb:
        return "software"
    return "static" if effect in ("static", "off") else "unsupported"


__all__ = [
    "EFFECTS", "DIRECTIONS", "Keyboard", "ECKeyboard", "LedClassKeyboard",
    "Ite829xKeyboard", "open_keyboard", "spread", "col",
]
