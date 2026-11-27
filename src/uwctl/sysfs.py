"""Small helpers for sysfs access.  ``UWCTL_SYSFS_ROOT`` lets tests point the
whole program at a fake /sys tree."""

from __future__ import annotations

import glob
import os


def root() -> str:
    return os.environ.get("UWCTL_SYSFS_ROOT", "/sys")


def path(*parts: str) -> str:
    return os.path.join(root(), *parts)


def read(p: str, default: str | None = None) -> str | None:
    try:
        with open(p) as fh:
            return fh.read().strip()
    except OSError:
        return default


def read_int(p: str, default: int | None = None) -> int | None:
    v = read(p)
    if v is None:
        return default
    try:
        return int(v.split()[0], 0)
    except (ValueError, IndexError):
        return default


def write(p: str, value) -> None:
    with open(p, "w") as fh:
        fh.write(str(value))


def exists(p: str) -> bool:
    return os.path.exists(p)


def g(pattern: str) -> list[str]:
    return sorted(glob.glob(path(pattern)))


def dmi() -> dict[str, str]:
    keys = ("sys_vendor", "product_name", "product_sku", "board_vendor", "board_name", "bios_version")
    out = {}
    for k in keys:
        v = read(path("class/dmi/id", k))
        if v:
            out[k] = v
    return out


def module_loaded(name: str) -> bool:
    return exists(path("module", name))


def uniwill_platform_dir() -> str | None:
    """sysfs dir of the upstream uniwill-laptop driver (INOU0000 platform device)."""
    for d in g("bus/platform/devices/INOU0000:*"):
        if exists(os.path.join(d, "driver")):
            return d
    return None


def find_hwmon(name: str) -> str | None:
    for d in g("class/hwmon/hwmon*"):
        if read(os.path.join(d, "name")) == name:
            return d
    return None
