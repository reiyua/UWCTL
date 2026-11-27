"""Software lighting effects, for keyboards whose controller has no built-in
animation (EC single-zone RGB, LED-class keyboards)."""

from __future__ import annotations

import math

from . import colors as col
from .colors import RGB


def period_for_speed(speed: int) -> float:
    """speed 1 (slow) .. 10 (fast) -> animation period in seconds."""
    speed = max(1, min(10, speed))
    return 8.0 - (speed - 1) * (7.0 / 9.0)  # 8 s .. 1 s


class SoftwareEffect:
    def __init__(self, name: str, colors: list[RGB], speed: int, zones: int, direction: str = "right") -> None:
        self.name = name
        self.colors = colors or [(255, 255, 255)]
        self.period = period_for_speed(speed)
        self.zones = max(1, zones)
        self.reverse = direction in ("left", "up", "up_left", "down_left")

    def frame(self, t: float) -> tuple[list[RGB], float]:
        """Return (per-zone colours, brightness multiplier 0..1) at time t."""
        p = (t / self.period) % 1.0
        n = self.zones
        if self.name == "breathing":
            # cycle through the palette, one colour per breath
            idx = int(t / self.period) % len(self.colors)
            level = (1 - math.cos(2 * math.pi * p)) / 2
            return [col.scale(self.colors[idx], level)] * n, 1.0
        if self.name == "flash":
            idx = int(t / self.period) % len(self.colors)
            on = p < 0.5
            return [self.colors[idx] if on else (0, 0, 0)] * n, 1.0
        if self.name == "rainbow":
            return [col.hsv(p)] * n, 1.0
        if self.name in ("wave", "rainbow_wave"):
            out = []
            for z in range(n):
                off = z / n
                if self.reverse:
                    off = -off
                if self.name == "rainbow_wave" or len(self.colors) < 2:
                    out.append(col.hsv(p + off))
                else:
                    x = (p + off) % 1.0 * len(self.colors)
                    i = int(x)
                    a = self.colors[i % len(self.colors)]
                    b = self.colors[(i + 1) % len(self.colors)]
                    out.append(col.lerp(a, b, x - i))
            return out, 1.0
        return [self.colors[0]] * n, 1.0
