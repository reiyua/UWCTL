"""Performance profiles, battery charge limit, Fn-lock / Win-key lock and the
lightbar."""

from __future__ import annotations

import os

from . import colors as col
from . import constants as C
from . import sysfs

PROFILES = {
    "office": "Office (quiet, power saving)",
    "gaming": "Gaming (balanced)",
    "turbo": "Turbo (maximum performance)",
}
_PLATFORM_PROFILE_MAP = {
    "office": ("quiet", "low-power", "cool"),
    "gaming": ("balanced",),
    "turbo": ("performance", "balanced-performance"),
}


class PowerError(RuntimeError):
    pass


class Power:
    def __init__(self, ec) -> None:
        self.ec = ec
        self.platform_dir = sysfs.uniwill_platform_dir()

    # -- performance profile ------------------------------------------------
    def profile_backend(self) -> str | None:
        if self.ec is not None:
            return "ec"
        if sysfs.exists(sysfs.path("firmware/acpi/platform_profile")):
            return "platform_profile"
        return None

    def get_profile(self) -> str | None:
        b = self.profile_backend()
        if b == "ec":
            v = self.ec.read(C.MANUAL_FAN_CTRL) & C.PROFILE_MASK
            for name, val in C.PROFILE_VALUES.items():
                if v == val:
                    return name
            return None
        if b == "platform_profile":
            cur = sysfs.read(sysfs.path("firmware/acpi/platform_profile"))
            for name, opts in _PLATFORM_PROFILE_MAP.items():
                if cur in opts:
                    return name
        return None

    def set_profile(self, name: str) -> None:
        if name not in PROFILES:
            raise PowerError(f"unknown profile {name!r} (choose {', '.join(PROFILES)})")
        b = self.profile_backend()
        if b == "ec":
            self.ec.update_bits(C.MANUAL_FAN_CTRL, C.PROFILE_MASK, C.PROFILE_VALUES[name])
        elif b == "platform_profile":
            choices = (sysfs.read(sysfs.path("firmware/acpi/platform_profile_choices")) or "").split()
            for opt in _PLATFORM_PROFILE_MAP[name]:
                if opt in choices:
                    sysfs.write(sysfs.path("firmware/acpi/platform_profile"), opt)
                    return
            raise PowerError(f"platform_profile offers none of {_PLATFORM_PROFILE_MAP[name]}")
        else:
            raise PowerError("no way to change the performance profile on this machine")

    # -- battery ---------------------------------------------------------------
    @staticmethod
    def battery_dir() -> str | None:
        for d in sysfs.g("class/power_supply/BAT*"):
            if sysfs.exists(os.path.join(d, "charge_control_end_threshold")):
                return d
        return None

    def get_charge_limit(self) -> int | None:
        d = self.battery_dir()
        return sysfs.read_int(os.path.join(d, "charge_control_end_threshold")) if d else None

    def set_charge_limit(self, percent: int) -> None:
        d = self.battery_dir()
        if not d:
            raise PowerError("battery charge limit needs a kernel driver that exposes "
                             "charge_control_end_threshold (uniwill-laptop or tuxedo-drivers)")
        if not 40 <= percent <= 100:
            raise PowerError("charge limit must be between 40 and 100 %")
        sysfs.write(os.path.join(d, "charge_control_end_threshold"), percent)

    # -- Fn lock / super key (upstream uniwill-laptop sysfs only) ---------------
    def _attr(self, name: str) -> str | None:
        if self.platform_dir:
            p = os.path.join(self.platform_dir, name)
            if sysfs.exists(p):
                return p
        return None

    def toggles(self) -> dict:
        out = {}
        for key, attr in (("fn_lock", "fn_lock"), ("super_key", "super_key_enable"),
                          ("touchpad_toggle", "touchpad_toggle_enable")):
            p = self._attr(attr)
            if p:
                out[key] = sysfs.read(p) == "1"
        return out

    def set_toggle(self, key: str, on: bool) -> None:
        attr = {"fn_lock": "fn_lock", "super_key": "super_key_enable",
                "touchpad_toggle": "touchpad_toggle_enable"}.get(key)
        p = self._attr(attr) if attr else None
        if not p:
            raise PowerError(f"{key} needs the uniwill-laptop kernel driver (Linux 6.19+)")
        sysfs.write(p, "1" if on else "0")


class Lightbar:
    """Front/side lightbar found on some models."""

    def __init__(self, ec) -> None:
        self.ec = ec
        leds = [d for d in sysfs.g("class/leds/*") if os.path.basename(d).endswith(":status")
                and os.path.basename(d).startswith("uniwill")]
        self.led = leds[0] if leds else None

    def available(self) -> bool:
        if self.led:
            return True
        if self.ec is None:
            return False
        try:
            return bool(self.ec.read(C.SUPPORT_1) & 0x40)
        except Exception:
            return False

    def backend(self) -> str | None:
        return "leds" if self.led else ("ec" if self.available() else None)

    def set(self, enabled: bool, rgb=(0, 255, 255), rainbow: bool = False) -> None:
        if self.led:
            trig = os.path.join(self.led, "trigger")
            if rainbow:
                sysfs.write(trig, "uniwill-rainbow")
                return
            sysfs.write(trig, "none")
            mx = sysfs.read(os.path.join(self.led, "multi_max_intensity"))
            maxv = max(int(x) for x in mx.split()) if mx else C.LIGHTBAR_MAX
            vals = " ".join(str(round(c * maxv / 255)) for c in rgb)
            sysfs.write(os.path.join(self.led, "multi_intensity"), vals)
            sysfs.write(os.path.join(self.led, "brightness"), 1 if enabled else 0)
            return
        if not self.available():
            raise PowerError("no lightbar on this machine")
        ec = self.ec
        vals = [round(c * C.LIGHTBAR_MAX / 255) for c in col.parse(rgb)]
        for regs in (C.LIGHTBAR_AC_RGB, C.LIGHTBAR_BAT_RGB):
            for reg, v in zip(regs, vals):
                ec.write(reg, v)
        mask = C.LIGHTBAR_S0_OFF | C.LIGHTBAR_WELCOME | C.LIGHTBAR_APP_EXISTS
        val = C.LIGHTBAR_APP_EXISTS
        if not enabled:
            val |= C.LIGHTBAR_S0_OFF
        elif rainbow:
            val |= C.LIGHTBAR_WELCOME
        for reg in (C.LIGHTBAR_AC_CTRL, C.LIGHTBAR_BAT_CTRL):
            ec.update_bits(reg, mask, val)
