"""Colour helpers."""

from __future__ import annotations

import colorsys

RGB = tuple[int, int, int]

NAMED = {
    "red": (255, 0, 0),
    "orange": (255, 165, 0),
    "yellow": (255, 255, 0),
    "green": (0, 255, 0),
    "cyan": (0, 255, 255),
    "blue": (0, 0, 255),
    "purple": (139, 0, 255),
    "magenta": (255, 0, 255),
    "pink": (255, 105, 180),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "off": (0, 0, 0),
}

#: The seven colours the Windows app uses as its default palette.
DEFAULT_PALETTE: list[RGB] = [
    (255, 0, 0),
    (255, 165, 0),
    (255, 255, 0),
    (0, 255, 0),
    (0, 0, 255),
    (0, 255, 255),
    (139, 0, 255),
]


def parse(value) -> RGB:
    """Accept '#rrggbb', 'rrggbb', 'r,g,b', a colour name or a 3-sequence."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        r, g, b = (int(x) for x in value)
    else:
        s = str(value).strip().lower()
        if s in NAMED:
            return NAMED[s]
        if "," in s:
            r, g, b = (int(x) for x in s.split(","))
        else:
            s = s.lstrip("#")
            if len(s) == 3:
                s = "".join(c * 2 for c in s)
            if len(s) != 6:
                raise ValueError(f"not a colour: {value!r}")
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    for c in (r, g, b):
        if not 0 <= c <= 255:
            raise ValueError(f"colour component out of range: {value!r}")
    return (r, g, b)


def to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def scale(rgb: RGB, factor: float) -> RGB:
    f = max(0.0, min(1.0, factor))
    return tuple(int(round(c * f)) for c in rgb)  # type: ignore[return-value]


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def lerp(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))  # type: ignore[return-value]
