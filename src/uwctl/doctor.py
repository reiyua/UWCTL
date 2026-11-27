"""``uwctl doctor`` - work out what this laptop supports and what is missing."""

from __future__ import annotations

import os
import platform

from . import __version__
from . import constants as C
from . import sysfs
from .ec import find_inou_path, find_wmi_method
from .keyboard import LedClassKeyboard, find_ite829x


def _os_release() -> str:
    try:
        with open("/etc/os-release") as fh:
            kv = dict(line.rstrip().split("=", 1) for line in fh if "=" in line)
        return kv.get("PRETTY_NAME", "").strip('"')
    except OSError:
        return "unknown"


def _usb(vendor: str | None = None) -> list[str]:
    out = []
    for d in sysfs.g("bus/usb/devices/*"):
        vid = sysfs.read(os.path.join(d, "idVendor"))
        if not vid or vid.lower() == "1d6b":  # skip root hubs
            continue
        if vendor and vid.lower() != vendor:
            continue
        pid = sysfs.read(os.path.join(d, "idProduct")) or "?"
        prod = sysfs.read(os.path.join(d, "product")) or ""
        out.append(f"{vid}:{pid} {prod}".strip())
    return sorted(set(out))


def _usb_ite() -> list[str]:
    return _usb("048d")


def _ec_info(ec) -> dict:
    info: dict = {}
    try:
        rom = bytes(ec.read(C.ROMID_START + i) for i in range(14))
        info["rom_id"] = rom.decode("ascii", "replace").strip("\x00 ")
        info["project_id"] = f"{ec.read(C.PROJECT_ID):#04x}"
        kbd = ec.read(C.KBD_STATUS)
        s1, s2 = ec.read(C.SUPPORT_1), ec.read(C.SUPPORT_2)
        info["keyboard"] = "white" if kbd & C.KBD_WHITE_ONLY else ("rgb" if s2 & 0x04 else "unknown")
        info["lightbar"] = bool(s1 & 0x40)
        info["fan_boost"] = bool(s1 & 0x80)
        info["universal_fan_control"] = bool(ec.read(C.FAN_CTRL) & C.UNIVERSAL_FAN_CTRL_BIT)
        info["has_gpu"] = bool(ec.read(C.SYSTEM_ID) & 0x80)
        info["cpu_temp"] = ec.read(C.CPU_TEMP)
    except Exception as exc:
        info["error"] = str(exc)
    return info


def report(ctl=None) -> dict:
    r: dict = {"uwctl": __version__}
    r["system"] = {"os": _os_release(), "kernel": platform.release(), **sysfs.dmi()}
    inou = find_inou_path(sysfs.root())
    wmi = find_wmi_method(sysfs.root())
    r["acpi"] = {
        "inou0000": inou,
        "wmi_ec_method": wmi,
        "acpi_call": os.path.exists("/proc/acpi/call"),
    }
    plat = sysfs.uniwill_platform_dir()
    tux = [m for m in ("tuxedo_keyboard", "uniwill_wmi", "tuxedo_io", "ite_829x", "ite_8291")
           if sysfs.module_loaded(m)]
    r["drivers"] = {
        "uniwill_laptop": bool(plat),
        "tuxedo_modules": tux,
        "hwmon_uniwill": bool(sysfs.find_hwmon("uniwill")),
    }
    r["keyboard_devices"] = {
        "ite829x_hidraw": find_ite829x(),
        "usb_ite": _usb_ite(),
        "led_class": [os.path.basename(d) for d in LedClassKeyboard.discover()][:6],
        "usb_all": _usb(),
    }
    if ctl is not None and ctl.ec is not None:
        r["ec"] = {"backend": ctl.ec.name, **_ec_info(ctl.ec)}
        r["active"] = {
            "keyboard": ctl.keyboard.info() if ctl.keyboard else None,
            "fan_modes": ctl.fans.capabilities()["modes"],
            "profile_backend": ctl.power.profile_backend(),
            "lightbar": ctl.lightbar.backend(),
        }
    elif ctl is not None:
        r["ec"] = {"error": ctl.ec_error}

    tips = []
    if not inou and not wmi:
        tips.append("Neither the INOU0000 ACPI device nor the Uniwill WMI EC interface was found: "
                    "this is probably not a Uniwill/Tongfang laptop. Fan/EC features will not work.")
    elif not r["acpi"]["acpi_call"]:
        tips.append("Install acpi_call for EC access - Debian/Ubuntu: 'sudo apt install "
                    "acpi-call-dkms', Arch: 'sudo pacman -S acpi_call' (or acpi_call-dkms / "
                    "acpi_call-lts), then 'sudo modprobe acpi_call'.")
    if tux:
        tips.append("tuxedo-drivers modules are loaded. Don't run TUXEDO Control Center (tccd) "
                    "at the same time as uwctld - both would fight over the fans.")
    if not plat and inou:
        tips.append("Optional: Linux 6.19+ has an upstream 'uniwill-laptop' driver that adds Fn-lock, "
                    "Win-key lock, battery charge limit and hwmon sensors. It only auto-loads on "
                    "listed models; 'sudo modprobe uniwill-laptop force=1' tries it on others.")
    ec = r.get("ec", {})
    if ec.get("universal_fan_control") is False:
        kind = ctl.fans.capabilities().get("custom_kind") if ctl is not None and ctl.ec else None
        if kind == "software":
            tips.append("This EC has no built-in fan-curve tables, so custom fan curves run in "
                        "software: uwctld sets the fan speed every 2 s and hands the fans back "
                        "to the firmware whenever it stops.")
        else:
            tips.append("This EC has no universal fan control, so custom fan curves are not "
                        "available. Auto and Max fan modes still work.")
    if not r["keyboard_devices"]["ite829x_hidraw"] and r["keyboard_devices"]["usb_ite"]:
        tips.append("An ITE USB keyboard controller other than 829x was found "
                    f"({', '.join(r['keyboard_devices']['usb_ite'])}). Per-key RGB for it needs "
                    "tuxedo-drivers (ite_8291 / ite_8297) or OpenRGB; uwctl will use its LED "
                    "devices if a driver creates them.")
    r["tips"] = tips
    return r


def format_report(r: dict) -> str:
    lines = [f"uwctl {r.get('uwctl')} - system report", ""]

    def section(title, d):
        lines.append(f"[{title}]")
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, list):
                    v = ", ".join(map(str, v)) or "-"
                elif isinstance(v, dict):
                    v = ", ".join(f"{a}={b}" for a, b in v.items() if not isinstance(b, (list, dict)))
                lines.append(f"  {k:24} {v}")
        lines.append("")

    for key in ("system", "acpi", "drivers", "keyboard_devices", "ec", "active"):
        if key in r:
            section(key, r[key])
    if r.get("tips"):
        lines.append("[tips]")
        for t in r["tips"]:
            lines.append(f"  * {t}")
    return "\n".join(lines)
