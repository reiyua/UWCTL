"""Glue between saved settings and the hardware backends.  Used by the
daemon (normal case) and by ``uwctl --direct`` when run as root."""

from __future__ import annotations

import logging
import os
import threading
import time

from . import colors as col
from . import constants as C
from . import sysfs
from .ec import BaseEC, ECError, ECUnavailable, open_ec
from .effects import SoftwareEffect
from .fans import PRESETS, FanController, FanError, FanSmoother, percent_to_direct
from .keyboard import EFFECTS, Keyboard, KeyboardError, open_keyboard
from .power import PROFILES, Lightbar, Power, PowerError
from .state import StateStore

log = logging.getLogger("uwctl")

# failsafe thresholds while a custom fan curve is active
FAILSAFE_CPU = 95
FAILSAFE_GPU = 92
FAILSAFE_RELEASE = 78
# software fan curve timing
FAN_TICK = 2.0             # seconds between updates
FAN_REWRITE = 30.0         # re-send unchanged duty this often
FAN_REFRESH = 50 * 60.0    # re-engage full fan mode this often (EC drops it otherwise)


class SoftwareFan:
    """State of a software fan curve (ECs without universal fan control)."""

    def __init__(self, cpu_curve, gpu_curve, same_speed: bool = True) -> None:
        self.cpu = FanSmoother(cpu_curve)
        self.gpu = FanSmoother(gpu_curve)
        self.same_speed = same_speed
        self.engaged_at: float | None = None
        self.last: tuple[int, int] | None = None
        self.last_write = 0.0

    def duties(self, cpu_temp, gpu_temp) -> tuple[int, int]:
        if cpu_temp is None:  # can't read the temperature: be safe
            return (C.FAN_SPEED_MAX, C.FAN_SPEED_MAX)
        c = self.cpu.update(cpu_temp)
        # the dGPU reports 0 while powered down (hybrid graphics): follow the CPU
        g = self.gpu.update(gpu_temp) if gpu_temp else c
        if self.same_speed:  # shared heat pipes: run both fans together
            c = g = max(c, g)
        return (percent_to_direct(c), percent_to_direct(g))


def on_ac_power() -> bool | None:
    for d in sysfs.g("class/power_supply/*"):
        if sysfs.read(os.path.join(d, "type")) == "Mains":
            v = sysfs.read(os.path.join(d, "online"))
            if v is not None:
                return v == "1"
    return None


class Controller:
    def __init__(self, store: StateStore, ec: BaseEC | None = None, dry_run: bool = False) -> None:
        self.store = store
        self.lock = threading.RLock()
        self.ec_error = None
        if ec is None:
            try:
                ec = open_ec(dry_run=dry_run)
            except ECUnavailable as exc:
                self.ec_error = str(exc)
                log.warning("EC unavailable: %s", exc)
        self.ec = ec
        self.fans = FanController(ec)
        self.power = Power(ec)
        self.lightbar = Lightbar(ec)
        self.keyboard: Keyboard | None = None
        self._open_keyboard()
        self.effect: SoftwareEffect | None = None
        self.effect_t0 = time.monotonic()
        self.cpu_table = None
        self.failsafe_active = False
        self.last_ac = on_ac_power()
        self.errors: dict[str, str] = {}
        #: True inside uwctld - software fan curves need a long-running process
        self.daemon_mode = False
        self.sw: SoftwareFan | None = None

    # ------------------------------------------------------------------ setup
    def _open_keyboard(self) -> None:
        kcfg = self.store.state["keyboard"]
        if kcfg["backend"] == "none":
            self.keyboard = None
            return
        self.keyboard = open_keyboard(self.ec, kcfg["backend"], kcfg["ec_levels"])

    def capabilities(self) -> dict:
        kb = self.keyboard.info() if self.keyboard else None
        fan_caps = self.fans.capabilities()
        return {
            "ec": self.ec.name if self.ec else None,
            "ec_error": self.ec_error,
            "keyboard": kb,
            "fans": fan_caps,
            "profiles": list(PROFILES) if self.power.profile_backend() else [],
            "profile_backend": self.power.profile_backend(),
            "lightbar": self.lightbar.backend(),
            "charge_limit": self.power.battery_dir() is not None,
            "toggles": sorted(self.power.toggles()),
            "effects": EFFECTS,
            "presets": PRESETS,
        }

    # ------------------------------------------------------------- keyboard
    def _kbd_brightness(self) -> float:
        k = self.store.state["keyboard"]
        pct = k["brightness"]
        if k["battery_brightness"] is not None and on_ac_power() is False:
            pct = k["battery_brightness"]
        return pct / 100

    def apply_keyboard(self) -> None:
        kb = self.keyboard
        self.effect = None
        if kb is None:
            return
        k = self.store.state["keyboard"]
        colors = [col.parse(c) for c in k["colors"]]
        bri = self._kbd_brightness()
        eff = k["effect"]
        if eff == "off" or bri == 0:
            kb.off()
        elif eff == "static" or not kb.is_rgb and eff != "breathing":
            kb.set_colors(colors, bri)
        elif eff in kb.hw_effects:
            hw_colors = [] if k["random_colors"] else colors
            kb.set_hw_effect(eff, hw_colors, bri, k["speed"], k["direction"])
        else:
            if eff in ("rainbow", "rainbow_wave"):
                colors = []
            elif k["random_colors"]:
                colors = list(col.DEFAULT_PALETTE)
            self.effect = SoftwareEffect(eff, colors, k["speed"], kb.zones, k["direction"])
            self.effect_t0 = time.monotonic()
            self.render_effect()

    def render_effect(self) -> None:
        if not self.effect or not self.keyboard:
            return
        frame, mult = self.effect.frame(time.monotonic() - self.effect_t0)
        bri = self._kbd_brightness() * mult
        kb = self.keyboard
        if not kb.is_rgb:  # white keyboard: breathe with the brightness levels
            level = max(c for c in frame[0]) / 255
            kb.set_brightness(bri * level)
        else:
            kb.set_colors(frame, bri if bri > 0 else 0.01)

    # ---------------------------------------------------------------- fans
    def apply_fans(self) -> None:
        f = self.store.state["fans"]
        if self.ec is None:
            raise FanError(self.ec_error or "no EC access")
        self.sw = None
        self.failsafe_active = False
        self.cpu_table = None
        if f["mode"] == "auto":
            self.fans.set_auto()
        elif f["mode"] == "max":
            self.fans.set_max()
        else:
            kind = self.fans.capabilities()["custom_kind"]
            if kind == "hardware":
                self.cpu_table, _ = self.fans.set_custom(f["cpu_curve"], f["gpu_curve"])
            elif kind == "software":
                if not self.daemon_mode:
                    raise FanError("software fan curves need the uwctld service running "
                                   "(sudo systemctl start uwctld)")
                self.sw = SoftwareFan(f["cpu_curve"], f["gpu_curve"], f["same_speed"])
                self.fan_tick()
            else:
                raise FanError("custom fan curves are not supported on this laptop")

    def fan_tick(self) -> None:
        """One step of the software fan curve (called every FAN_TICK s)."""
        sw = self.sw
        if sw is None or self.ec is None:
            return
        with self.lock:
            try:
                cpu = self.ec.read(C.CPU_TEMP)
                gpu = self.ec.read(C.GPU_TEMP) or None
            except ECError as exc:
                log.error("fan curve: cannot read temperatures (%s) - fans to max", exc)
                cpu = gpu = None
            hot = cpu is None or cpu >= FAILSAFE_CPU or (gpu or 0) >= FAILSAFE_GPU
            if hot and not self.failsafe_active:
                log.warning("failsafe: CPU %s °C / GPU %s °C - fans to max", cpu, gpu)
                self.failsafe_active = True
            elif self.failsafe_active and cpu is not None and cpu < FAILSAFE_RELEASE \
                    and (gpu or 0) < FAILSAFE_RELEASE:
                log.info("failsafe released")
                self.failsafe_active = False
            d = sw.duties(cpu, gpu)
            if self.failsafe_active:
                d = (C.FAN_SPEED_MAX, C.FAN_SPEED_MAX)
            now = time.monotonic()
            try:
                boost = self.fans.boost_on()
                if sw.engaged_at is None or not boost or now - sw.engaged_at > FAN_REFRESH:
                    if sw.engaged_at is not None and not boost:
                        log.info("full fan mode was switched off (Fn key or resume?) - re-engaging")
                    self.fans.engage_direct(*d)
                    sw.engaged_at = sw.last_write = now
                    sw.last = d
                elif d != sw.last or now - sw.last_write > FAN_REWRITE:
                    self.fans.write_direct(*d)
                    sw.last, sw.last_write = d, now
            except ECError as exc:
                log.error("fan curve: %s", exc)

    def release_fans(self) -> None:
        """Hand the fans back to the firmware (daemon shutdown)."""
        with self.lock:
            if self.sw is not None:
                self.sw = None
                try:
                    self.fans.set_auto()
                    log.info("software fan curve stopped - fans back on Auto")
                except (ECError, FanError) as exc:
                    log.error("could not return fans to Auto: %s", exc)

    # --------------------------------------------------------------- others
    def apply_profile(self) -> None:
        p = self.store.state["profile"]
        ac = on_ac_power()
        name = p["battery"] if ac is False and p["battery"] else p["ac"]
        if name:
            self.power.set_profile(name)

    def apply_lightbar(self) -> None:
        lb = self.store.state["lightbar"]
        if lb["managed"] and self.lightbar.available():
            self.lightbar.set(lb["enabled"], col.parse(lb["color"]), lb["rainbow"])

    def apply_battery(self) -> None:
        cl = self.store.state["battery"]["charge_limit"]
        if cl is not None:
            self.power.set_charge_limit(cl)

    def apply_toggles(self) -> None:
        for key, on in self.store.state["toggles"].items():
            self.power.set_toggle(key, on)

    SECTIONS = ("keyboard", "lightbar", "fans", "profile", "battery", "toggles")

    def apply(self, sections=None) -> dict[str, str]:
        """Apply saved settings; returns {section: error} for failures."""
        sections = sections or self.SECTIONS
        errors = {}
        with self.lock:
            for s in sections:
                try:
                    getattr(self, f"apply_{s}")()
                except (ECError, FanError, KeyboardError, PowerError, OSError) as exc:
                    errors[s] = str(exc)
                    log.error("applying %s failed: %s", s, exc)
        self.errors.update(errors)
        for s in sections:
            if s not in errors:
                self.errors.pop(s, None)
        return errors

    def update(self, partial: dict) -> dict:
        with self.lock:
            backend_before = self.store.state["keyboard"]["backend"]
            self.store.update(partial)
            if self.store.state["keyboard"]["backend"] != backend_before:
                self._open_keyboard()
            return self.apply([s for s in self.SECTIONS if s in partial])

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        with self.lock:
            sensors = self.fans.sensors()
            fan_mode = None
            if self.sw is not None:
                fan_mode = "custom"
            elif self.ec is not None:
                try:
                    fan_mode = self.fans.current_mode()
                except ECError:
                    pass
            profile = None
            try:
                profile = self.power.get_profile()
            except (ECError, OSError):
                pass
            return {
                "sensors": sensors,
                "fan_mode": fan_mode,
                "fan_control": "software" if self.sw is not None else
                               ("hardware" if fan_mode == "custom" else None),
                "failsafe": self.failsafe_active,
                "profile": profile,
                "on_ac": on_ac_power(),
                "charge_limit": self.power.get_charge_limit(),
                "toggles": self.power.toggles(),
                "errors": dict(self.errors),
            }

    # ------------------------------------------------------- periodic work
    def watchdog(self) -> None:
        """Called every few seconds by the daemon."""
        with self.lock:
            ac = on_ac_power()
            if ac != self.last_ac:
                self.last_ac = ac
                log.info("power source changed (on_ac=%s)", ac)
                self.apply(["keyboard", "profile"])
            if self.ec is None or self.store.state["fans"]["mode"] != "custom" or self.sw is not None:
                return  # software curves have their own failsafe in fan_tick()
            try:
                s = self.fans.sensors()
                cpu, gpu = s.get("cpu_temp") or 0, s.get("gpu_temp") or 0
                if not self.failsafe_active and (cpu >= FAILSAFE_CPU or gpu >= FAILSAFE_GPU):
                    log.warning("failsafe: %s/%s °C with custom curve - fans to max", cpu, gpu)
                    self.fans.set_max()
                    self.failsafe_active = True
                elif self.failsafe_active and cpu < FAILSAFE_RELEASE and gpu < FAILSAFE_RELEASE:
                    log.info("failsafe released - restoring custom curve")
                    self.apply(["fans"])
                elif not self.failsafe_active and not self.fans.custom_active(self.cpu_table):
                    log.info("EC dropped the custom fan tables - re-applying")
                    self.apply(["fans"])
            except (ECError, FanError) as exc:
                log.error("fan watchdog: %s", exc)
