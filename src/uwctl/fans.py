"""Fan control.

Modes
-----
``auto``    EC firmware default fan behaviour.
``max``     "Full fan mode" - both fans at maximum (EC 0x0751 bit 6).
``custom``  User fan curves.  Two implementations:

            *hardware* - loaded into the EC's *universal fan control* tables
            (0x0F00-0x0F5F).  The EC follows the curve itself, so it keeps
            working even if the daemon stops.  Needs an EC that advertises
            universal fan control (0x078E bit 6).

            *software* - for older ECs (e.g. 2020 Tongfang GK5 boards): the
            daemon switches the EC to full fan mode and writes the fan duty
            registers 0x1804 / 0x1809 itself every couple of seconds, the way
            TUXEDO's driver does on these models.  Only reachable through the
            WMI interface.  Fans go back to Auto whenever the daemon stops.

A curve is a list of ``(temperature_C, speed_percent)`` points.  The EC table
holds 16 zones per fan of ``start_temp, end_temp, speed``; the curve is turned
into a staircase that never runs the fan slower than the curve asks for.
"""

from __future__ import annotations

import os

from . import constants as C
from . import sysfs

CPU_T_MAX = 115
GPU_T_MAX = 120
#: Above this temperature every curve is forced to 100 %.
SAFETY_TEMP = 90
#: Fans can't spin reliably below ~25 %; values below are rounded to off/25 %.
MIN_ON_PERCENT = 25

PRESETS: dict[str, list[tuple[int, int]]] = {
    "silent": [(45, 0), (55, 25), (65, 35), (75, 55), (85, 80), (90, 100)],
    "balanced": [(40, 0), (50, 25), (60, 40), (70, 60), (80, 80), (88, 100)],
    "performance": [(30, 25), (45, 40), (60, 60), (70, 80), (80, 100)],
}


class FanError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# curve maths (pure functions, unit tested)
# ---------------------------------------------------------------------------
def normalize_curve(points, safety_temp: int = SAFETY_TEMP) -> list[tuple[int, int]]:
    """Sort, clamp, make speeds non-decreasing and enforce the safety point."""
    if not points:
        raise FanError("fan curve needs at least one point")
    pts: dict[int, int] = {}
    for t, s in points:
        t = int(round(float(t)))
        s = int(round(float(s)))
        if not 0 <= t <= 110:
            raise FanError(f"curve temperature {t} °C outside 0-110")
        if not 0 <= s <= 100:
            raise FanError(f"curve speed {s} % outside 0-100")
        pts[t] = max(s, pts.get(t, 0))
    out: list[tuple[int, int]] = []
    running = 0
    for t in sorted(pts):
        running = max(running, pts[t])
        if t >= safety_temp:
            running = 100
        out.append((t, running))
    if out[-1][1] < 100 or out[-1][0] > safety_temp:
        out = [p for p in out if p[0] < safety_temp] + [(safety_temp, 100)]
    if len(out) > 12:
        raise FanError("fan curve may have at most 12 points")
    return out


def curve_value(points: list[tuple[int, int]], temp: float) -> float:
    """Piecewise-linear interpolation, flat beyond the ends."""
    if temp <= points[0][0]:
        return float(points[0][1])
    for (t0, s0), (t1, s1) in zip(points, points[1:]):
        if temp <= t1:
            return s0 + (s1 - s0) * (temp - t0) / (t1 - t0)
    return float(points[-1][1])


def _quantize(percent: float) -> int:
    """Percent -> EC duty (0..200) with the minimum-speed rule."""
    p = max(0.0, min(100.0, percent))
    if p < MIN_ON_PERCENT / 2:
        p = 0
    elif p < MIN_ON_PERCENT:
        p = MIN_ON_PERCENT
    raw = int(round(p * C.FAN_SPEED_MAX / 100))
    # Writing 0 makes the EC spin the fan at 30 % for three minutes before
    # stopping; 1 stops it straight away on 2020+ fans (see tuxedo-drivers).
    return raw if raw > 0 else 1


def curve_to_table(points, t_max: int = CPU_T_MAX, safety_temp: int = SAFETY_TEMP,
                   max_step: float = 4.0) -> list[tuple[int, int, int]]:
    """Return 16 ``(start_temp, end_temp, duty)`` zones for the EC."""
    pts = normalize_curve(points, safety_temp)
    bounds = sorted({0, t_max, *[t for t, _ in pts if 0 < t < t_max]})

    def zone_speed(lo: int, hi: int) -> float:
        # conservative: use the curve value at the hot end of the zone
        return curve_value(pts, hi)

    # split the steepest zones until we run out of zones or steps are small
    while len(bounds) - 1 < C.FAN_TABLE_LEN:
        best, best_delta = None, max_step
        for lo, hi in zip(bounds, bounds[1:]):
            delta = curve_value(pts, hi) - curve_value(pts, lo)
            if hi - lo >= 2 and delta > best_delta:
                best, best_delta = (lo, hi), delta
        if best is None:
            break
        bounds.append((best[0] + best[1]) // 2)
        bounds.sort()

    zones = [(lo, hi, _quantize(zone_speed(lo, hi))) for lo, hi in zip(bounds, bounds[1:])]
    # merge neighbours with identical duty
    merged: list[list[int]] = []
    for lo, hi, duty in zones:
        if merged and merged[-1][2] == duty:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi, duty])
    if len(merged) > C.FAN_TABLE_LEN:  # too many distinct steps: coarsen
        while len(merged) > C.FAN_TABLE_LEN:
            i = min(range(len(merged) - 1), key=lambda k: merged[k + 1][2] - merged[k][2])
            merged[i][1] = merged[i + 1][1]
            merged[i][2] = merged[i + 1][2]
            del merged[i + 1]
    table = [tuple(z) for z in merged]
    # pad with unreachable dummy zones at full speed, like tuxedo-drivers does
    last_end = table[-1][1]
    k = 0
    while len(table) < C.FAN_TABLE_LEN:
        table.append((last_end + k, last_end + k + 1, C.FAN_SPEED_MAX))
        k += 1
    return table  # type: ignore[return-value]


def percent_to_direct(percent: float) -> int:
    """Percent -> duty for the direct registers (0 really means off here)."""
    p = max(0.0, min(100.0, percent))
    if p < MIN_ON_PERCENT / 2:
        return 0
    if p < MIN_ON_PERCENT:
        p = MIN_ON_PERCENT
    return int(round(p * C.FAN_SPEED_MAX / 100))


class FanSmoother:
    """Turns a curve + noisy temperature into a calm fan speed: speeds up
    straight away, slows down only once the temperature has dropped
    ``hysteresis`` °C below the point that asked for the current speed, and
    then by at most ``step_down`` % per update."""

    def __init__(self, curve, hysteresis: float = 3.0, step_down: float = 5.0) -> None:
        self.pts = normalize_curve(curve)
        self.hyst = hysteresis
        self.step_down = step_down
        self.current: float | None = None

    def update(self, temp: float | None) -> float:
        if temp is None:
            return self.current if self.current is not None else 100.0
        want = curve_value(self.pts, temp)
        if self.current is None or want >= self.current:
            self.current = want
        else:
            relaxed = curve_value(self.pts, temp + self.hyst)
            if relaxed < self.current:
                self.current = max(relaxed, self.current - self.step_down)
        return self.current


def table_value(table, temp: int) -> int:
    """Duty the EC should pick for ``temp`` (used by tests and the UI)."""
    for start, end, duty in table:
        if start <= temp <= end:
            return duty
    return C.FAN_SPEED_MAX


# ---------------------------------------------------------------------------
# hardware
# ---------------------------------------------------------------------------
class FanController:
    def __init__(self, ec) -> None:
        self.ec = ec
        self.hwmon = sysfs.find_hwmon("uniwill")

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict:
        caps = {"universal": False, "fans": 0, "boost": False, "has_gpu": False}
        if self.ec is not None:
            try:
                caps["universal"] = bool(self.ec.read(C.FAN_CTRL) & C.UNIVERSAL_FAN_CTRL_BIT)
                caps["boost"] = True
                caps["has_gpu"] = bool(self.ec.read(C.SYSTEM_ID) & 0x80)
            except Exception:
                pass
        s = self.sensors()
        caps["fans"] = sum(1 for k in ("fan1_rpm", "fan2_rpm") if s.get(k) is not None)
        if caps["fans"] == 0 and self.ec is not None:
            caps["fans"] = 2 if caps["has_gpu"] else 1
        modes = ["auto"]
        if caps["boost"]:
            modes.append("max")
        caps["custom_kind"] = None
        if caps["universal"]:
            caps["custom_kind"] = "hardware"
        elif caps["boost"] and self.direct_capable():
            caps["custom_kind"] = "software"
        if caps["custom_kind"]:
            modes.append("custom")
        caps["modes"] = modes
        return caps

    def direct_capable(self) -> bool:
        return self.ec is not None and self.ec.max_addr >= C.PWM_2_DIRECT

    # -- sensors -----------------------------------------------------------
    def sensors(self) -> dict:
        out: dict = {}
        if self.hwmon:
            h = self.hwmon
            t1 = sysfs.read_int(os.path.join(h, "temp1_input"))
            t2 = sysfs.read_int(os.path.join(h, "temp2_input"))
            out["cpu_temp"] = t1 / 1000 if t1 is not None else None
            out["gpu_temp"] = t2 / 1000 if t2 is not None else None
            out["fan1_rpm"] = sysfs.read_int(os.path.join(h, "fan1_input"))
            out["fan2_rpm"] = sysfs.read_int(os.path.join(h, "fan2_input"))
            for i in (1, 2):
                pwm = sysfs.read_int(os.path.join(h, f"pwm{i}"))
                out[f"fan{i}_duty"] = round(pwm * 100 / 255) if pwm is not None else None
            out["source"] = "hwmon"
            return out
        if self.ec is not None:
            try:
                e = self.ec
                out["cpu_temp"] = e.read(C.CPU_TEMP)
                gpu = e.read(C.GPU_TEMP)
                out["gpu_temp"] = gpu if gpu else None
                r1 = e.read_be16(C.MAIN_FAN_RPM_HI, C.MAIN_FAN_RPM_LO)
                r2 = e.read_be16(C.SECOND_FAN_RPM_HI, C.SECOND_FAN_RPM_LO)
                out["fan1_rpm"] = r1 if r1 != 0xFFFF else None
                out["fan2_rpm"] = r2 if r2 != 0xFFFF else None
                out["fan1_duty"] = round(e.read(C.PWM_1) * 100 / C.FAN_SPEED_MAX)
                out["fan2_duty"] = round(e.read(C.PWM_2) * 100 / C.FAN_SPEED_MAX)
                out["source"] = "ec"
                return out
            except Exception as exc:  # pragma: no cover - hardware path
                out["error"] = str(exc)
        out.update(_generic_cpu_temp())
        return out

    # -- modes -------------------------------------------------------------
    def _require_ec(self):
        if self.ec is None:
            raise FanError("fan control needs EC access (acpi_call)")
        return self.ec

    def _set_boost(self, on: bool) -> None:
        self._require_ec().update_bits(C.MANUAL_FAN_CTRL, C.FAN_MODE_BOOST, C.FAN_MODE_BOOST if on else 0)

    def _custom_tables(self, on: bool) -> None:
        ec = self._require_ec()
        if on:
            ec.update_bits(C.UNIVERSAL_FAN_CTRL, C.SPLIT_TABLES_BIT, C.SPLIT_TABLES_BIT)
        else:
            ec.update_bits(C.AP_OEM_6, C.ENABLE_UNIVERSAL_FAN_CTRL_BIT, 0)
            ec.update_bits(C.UNIVERSAL_FAN_CTRL, C.SPLIT_TABLES_BIT, 0)

    def set_auto(self) -> None:
        self._custom_tables(False)
        self._set_boost(False)

    def set_max(self) -> None:
        self._custom_tables(False)
        self._set_boost(True)
        if self.direct_capable():
            # the duty registers may still hold a lower value from a software curve
            self.write_direct(C.FAN_SPEED_MAX, C.FAN_SPEED_MAX)

    # -- direct (software curve) control -----------------------------------
    def write_direct(self, duty1: int, duty2: int) -> None:
        ec = self._require_ec()
        ec.write(C.PWM_1_DIRECT, max(0, min(C.FAN_SPEED_MAX, duty1)))
        ec.write(C.PWM_2_DIRECT, max(0, min(C.FAN_SPEED_MAX, duty2)))

    def engage_direct(self, duty1: int, duty2: int, repeats: int = 10) -> None:
        """Enter full fan mode and pin the duty before the fans ramp to max.
        Also used every ~50 min to refresh direct control (tuxedo-drivers does
        the same: the EC drops back after a while otherwise)."""
        ec = self._require_ec()
        with ec.lock:
            self._set_boost(False)
            self._set_boost(True)
            for _ in range(repeats):
                self.write_direct(duty1, duty2)

    def boost_on(self) -> bool:
        return bool(self._require_ec().read(C.MANUAL_FAN_CTRL) & C.FAN_MODE_BOOST)

    def write_table(self, which: str, table) -> None:
        ec = self._require_ec()
        base = {
            "cpu": (C.CPU_TEMP_END_TABLE, C.CPU_TEMP_START_TABLE, C.CPU_FAN_SPEED_TABLE),
            "gpu": (C.GPU_TEMP_END_TABLE, C.GPU_TEMP_START_TABLE, C.GPU_FAN_SPEED_TABLE),
        }[which]
        end_a, start_a, speed_a = base
        with ec.lock:
            for i, (start, end, duty) in enumerate(table):
                ec.write(end_a + i, end)
                ec.write(start_a + i, start)
                ec.write(speed_a + i, duty)
            # read back and fix anything the EC dropped
            for i, (start, end, duty) in enumerate(table):
                for addr, val in ((end_a + i, end), (start_a + i, start), (speed_a + i, duty)):
                    if ec.read(addr) != val:
                        ec.write_verify(addr, val)

    def set_custom(self, cpu_curve, gpu_curve=None) -> tuple[list, list]:
        if not self.capabilities()["universal"]:
            raise FanError("this EC has no universal fan control - custom curves unsupported")
        cpu_table = curve_to_table(cpu_curve, CPU_T_MAX)
        gpu_table = curve_to_table(gpu_curve or cpu_curve, GPU_T_MAX)
        self._set_boost(False)
        self._custom_tables(True)
        self.write_table("cpu", cpu_table)
        self.write_table("gpu", gpu_table)
        self._require_ec().update_bits(C.AP_OEM_6, C.ENABLE_UNIVERSAL_FAN_CTRL_BIT,
                                       C.ENABLE_UNIVERSAL_FAN_CTRL_BIT)
        return cpu_table, gpu_table

    def custom_active(self, cpu_table=None) -> bool:
        """True if the EC still holds our custom tables (it may reset them on
        suspend/resume or AC changes)."""
        ec = self._require_ec()
        if not ec.read(C.AP_OEM_6) & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT:
            return False
        if not ec.read(C.UNIVERSAL_FAN_CTRL) & C.SPLIT_TABLES_BIT:
            return False
        if cpu_table:
            for i in (0, len(cpu_table) // 2):
                if ec.read(C.CPU_FAN_SPEED_TABLE + i) != cpu_table[i][2]:
                    return False
        return True

    def current_mode(self) -> str:
        ec = self._require_ec()
        if ec.read(C.MANUAL_FAN_CTRL) & C.FAN_MODE_BOOST:
            return "max"
        if ec.read(C.AP_OEM_6) & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT:
            return "custom"
        return "auto"


def _generic_cpu_temp() -> dict:
    for name in ("coretemp", "k10temp", "zenpower"):
        h = sysfs.find_hwmon(name)
        if h:
            t = sysfs.read_int(os.path.join(h, "temp1_input"))
            if t is not None:
                return {"cpu_temp": t / 1000, "source": name}
    return {}
