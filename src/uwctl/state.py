"""Persistent settings (``/etc/uwctl/state.json``) and validation."""

from __future__ import annotations

import copy
import json
import os

from . import colors as col
from .fans import PRESETS, normalize_curve
from .keyboard import DIRECTIONS, EFFECTS
from .power import PROFILES

DEFAULT_PATH = os.environ.get("UWCTL_STATE", "/etc/uwctl/state.json")

DEFAULTS: dict = {
    "keyboard": {
        "effect": "static",
        "colors": ["#00ffff"],
        "brightness": 100,           # percent
        "battery_brightness": None,  # percent on battery, None = same as AC
        "speed": 5,                  # 1 (slow) .. 10 (fast)
        "direction": "right",
        "random_colors": False,      # effects use the built-in palette instead of 'colors'
        "backend": "auto",           # auto | ite829x | leds | ec | none
        "ec_levels": 4,              # brightness steps of EC keyboards (2 or 4)
    },
    "lightbar": {"enabled": True, "color": "#00ffff", "rainbow": False, "managed": False},
    "fans": {
        "mode": "auto",              # auto | max | custom
        "cpu_curve": PRESETS["balanced"],
        "gpu_curve": PRESETS["balanced"],
        "same_speed": True,          # software curves: run both fans at the higher speed
    },
    "profile": {"ac": None, "battery": None},  # office | gaming | turbo | None (leave alone)
    "battery": {"charge_limit": None},
    "toggles": {},                   # fn_lock / super_key / touchpad_toggle -> bool
}


class StateError(ValueError):
    pass


def _deep_merge(base: dict, upd: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _pct(v, name, allow_none=False):
    if v is None and allow_none:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise StateError(f"{name} must be a number") from None
    if not 0 <= v <= 100:
        raise StateError(f"{name} must be 0-100")
    return v


def validate(state: dict) -> dict:
    s = _deep_merge(DEFAULTS, state)
    k = s["keyboard"]
    if k["effect"] not in EFFECTS:
        raise StateError(f"unknown keyboard effect {k['effect']!r}")
    if isinstance(k["colors"], str):
        k["colors"] = [k["colors"]]
    try:
        k["colors"] = [col.to_hex(col.parse(c)) for c in k["colors"]][:8] or ["#ffffff"]
    except ValueError as exc:
        raise StateError(str(exc)) from None
    k["brightness"] = _pct(k["brightness"], "brightness")
    k["battery_brightness"] = _pct(k["battery_brightness"], "battery brightness", True)
    k["speed"] = max(1, min(10, int(k["speed"])))
    if k["direction"] not in DIRECTIONS:
        raise StateError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if k["backend"] not in ("auto", "ite829x", "leds", "ec", "none"):
        raise StateError("keyboard backend must be auto, ite829x, leds, ec or none")
    k["ec_levels"] = 2 if int(k["ec_levels"]) <= 2 else 4
    k["random_colors"] = bool(k["random_colors"])

    lb = s["lightbar"]
    lb["color"] = col.to_hex(col.parse(lb["color"]))
    lb["enabled"], lb["rainbow"], lb["managed"] = bool(lb["enabled"]), bool(lb["rainbow"]), bool(lb["managed"])

    f = s["fans"]
    if f["mode"] not in ("auto", "max", "custom"):
        raise StateError("fan mode must be auto, max or custom")
    try:
        f["cpu_curve"] = [list(p) for p in normalize_curve(f["cpu_curve"])]
        f["gpu_curve"] = [list(p) for p in normalize_curve(f["gpu_curve"])]
    except Exception as exc:
        raise StateError(f"bad fan curve: {exc}") from None
    f["same_speed"] = bool(f["same_speed"])

    for src in ("ac", "battery"):
        p = s["profile"].get(src)
        if p is not None and p not in PROFILES:
            raise StateError(f"profile must be one of {', '.join(PROFILES)}")

    cl = s["battery"]["charge_limit"]
    if cl is not None:
        cl = int(cl)
        if not 40 <= cl <= 100:
            raise StateError("charge limit must be 40-100")
        s["battery"]["charge_limit"] = cl

    s["toggles"] = {k2: bool(v) for k2, v in s["toggles"].items()
                    if k2 in ("fn_lock", "super_key", "touchpad_toggle")}
    return s


class StateStore:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self.state = self.load()

    def load(self) -> dict:
        try:
            with open(self.path) as fh:
                return validate(json.load(fh))
        except FileNotFoundError:
            return validate({})
        except (OSError, ValueError, StateError):
            # corrupt file: keep a copy and start from defaults
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return validate({})

    def update(self, partial: dict) -> dict:
        new = validate(_deep_merge(self.state, partial))
        self.state = new
        self.save()
        return new

    def save(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)
