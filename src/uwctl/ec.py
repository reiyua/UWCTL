"""Access to the Uniwill embedded controller.

On Windows the vendor app uses ACPIDriver.sys to call the ACPI methods
``ECRR`` (read) and ``ECRW`` (write) on the ``INOU0000`` ACPI device, or - on
older firmware without that device, like the 2020 Tongfang GK5 boards - the
WMI method ``GetSetULong``.  On Linux we call the very same methods through
the ``acpi_call`` kernel module
(packaged as ``acpi-call-dkms`` on Debian/Ubuntu and ``acpi_call`` /
``acpi_call-dkms`` on Arch).

A file-backed :class:`MockEC` is provided for tests and ``--dry-run``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time

from . import constants as C


class ECError(RuntimeError):
    pass


class ECUnavailable(ECError):
    pass


class BaseEC:
    """Byte-wide EC RAM access with a write allowlist."""

    name = "base"
    #: highest readable address for this access method
    max_addr = C.EC_MAX_ADDR

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # subclasses implement these two
    def _read(self, addr: int) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def _write(self, addr: int, value: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def read(self, addr: int) -> int:
        if not 0 <= addr <= self.max_addr:
            raise ECError(f"EC address out of range: {addr:#06x}")
        with self._lock:
            return self._read(addr) & 0xFF

    def write(self, addr: int, value: int) -> None:
        if addr not in C.WRITABLE:
            raise ECError(f"refusing to write EC register {addr:#06x} (not on allowlist)")
        if addr > self.max_addr:
            raise ECError(f"EC register {addr:#06x} is not reachable with the {self.name} interface")
        if not 0 <= value <= 0xFF:
            raise ECError(f"EC value out of range: {value}")
        with self._lock:
            self._write(addr, value)

    def write_verify(self, addr: int, value: int, retries: int = 3) -> None:
        for _ in range(retries):
            self.write(addr, value)
            if self.read(addr) == value:
                return
        raise ECError(f"EC register {addr:#06x} did not accept {value:#04x}")

    def update_bits(self, addr: int, mask: int, value: int) -> int:
        with self._lock:
            old = self.read(addr)
            new = (old & ~mask & 0xFF) | (value & mask)
            if new != old:
                self.write(addr, new)
            return new

    def read_be16(self, hi: int, lo: int) -> int:
        return (self.read(hi) << 8) | self.read(lo)

    @property
    def lock(self) -> threading.RLock:
        return self._lock


def find_inou_path(sysfs_root: str = "/sys") -> str | None:
    """Return the ACPI namespace path of the INOU0000 device, e.g. ``\\_SB_.INOU``."""
    for dev in sorted(glob.glob(os.path.join(sysfs_root, "bus/acpi/devices/INOU0000:*"))):
        try:
            with open(os.path.join(dev, "path")) as fh:
                path = fh.read().strip()
        except OSError:
            continue
        if path:
            return path
    return None


class AcpiCallEC(BaseEC):
    """EC access via /proc/acpi/call (acpi_call kernel module)."""

    name = "acpi_call"
    PROC = "/proc/acpi/call"
    #: The OEM software waits 6 ms after every EC access to avoid
    #: overwhelming the EC on the LPC bus; the kernel driver does the same.
    DELAY = 0.006

    def __init__(self, acpi_path: str | None = None, proc: str | None = None) -> None:
        super().__init__()
        self.proc = proc or self.PROC
        self.acpi_path = acpi_path or find_inou_path()
        if not self.acpi_path:
            raise ECUnavailable(
                "ACPI device INOU0000 not found - this does not look like a Uniwill laptop"
            )
        if not os.path.exists(self.proc):
            raise ECUnavailable(
                "/proc/acpi/call missing - install acpi_call "
                "(Debian: acpi-call-dkms, Arch: acpi_call or acpi_call-dkms) "
                "and run 'modprobe acpi_call'"
            )
        if not os.access(self.proc, os.W_OK):
            raise ECUnavailable("/proc/acpi/call needs root (run the uwctld service)")

    def _call(self, expr: str) -> str:
        with open(self.proc, "w") as fh:
            fh.write(expr)
        with open(self.proc) as fh:
            out = fh.read().strip("\x00\n ")
        time.sleep(self.DELAY)
        if out.startswith("Error"):
            raise ECError(f"{expr}: {out}")
        return out

    def _read(self, addr: int) -> int:
        out = self._call(f"{self.acpi_path}.ECRR {addr:#x}")
        try:
            val = int(out, 0)
        except ValueError as exc:
            raise ECError(f"unexpected ECRR reply {out!r}") from exc
        if val > 0xFF:
            raise ECError(f"ECRR({addr:#x}) failed: {val:#x}")
        return val

    def _write(self, addr: int, value: int) -> None:
        self._call(f"{self.acpi_path}.ECRW {addr:#x} {value:#x}")


WMI_EC_GUID = "ABBC0F6F-8EA1-11D1-00A0-C90629100000"


def find_wmi_method(sysfs_root: str = "/sys") -> str | None:
    """Return the ACPI method behind the Uniwill ``GetSetULong`` WMI method,
    e.g. ``\\_SB_.AMW0.WMBC``.

    Older Uniwill/Tongfang firmware (2020 and earlier) has no INOU0000 device;
    the Windows app then talks to the EC through this WMI interface instead."""
    wmi_dir = os.path.join(sysfs_root, "bus/wmi/devices")
    try:
        names = sorted(os.listdir(wmi_dir))
    except OSError:
        return None
    for name in names:
        if not name.upper().startswith(WMI_EC_GUID):
            continue
        dev = os.path.join(wmi_dir, name)
        try:
            with open(os.path.join(dev, "object_id")) as fh:
                obj = fh.read().strip()
        except OSError:
            continue
        m = re.search(r"/(PNP0C14:[0-9A-Fa-f]+)/", os.path.realpath(dev))
        if not obj or not m:
            continue
        try:
            with open(os.path.join(sysfs_root, "bus/acpi/devices", m.group(1), "path")) as fh:
                parent = fh.read().strip()
        except OSError:
            continue
        if parent:
            return f"{parent}.WM{obj}"
    return None


class WmiAcpiCallEC(AcpiCallEC):
    """EC access through the Uniwill WMI method ``GetSetULong`` (method id 4),
    called via acpi_call.  Input is an 8-byte buffer:
    ``addr_lo, addr_hi, data_lo, data_hi, 0, op, 0, 0`` with op 1 = read,
    0 = write.  The reply is a buffer whose first byte is the value read;
    0xFEFEFEFE means the EC did not answer."""

    name = "wmi"
    max_addr = 0xFFFF
    READ, WRITE = 1, 0
    FAILED = 0xFEFEFEFE

    def __init__(self, method: str | None = None, proc: str | None = None) -> None:
        BaseEC.__init__(self)
        self.proc = proc or self.PROC
        self.method = method or find_wmi_method()
        if not self.method:
            raise ECUnavailable("Uniwill WMI EC interface (ABBC0F6F...) not found")
        if not os.path.exists(self.proc):
            raise ECUnavailable(
                "/proc/acpi/call missing - install acpi_call "
                "(Debian: acpi-call-dkms, Arch: acpi_call or acpi_call-dkms) "
                "and run 'modprobe acpi_call'"
            )
        if not os.access(self.proc, os.W_OK):
            raise ECUnavailable("/proc/acpi/call needs root (run the uwctld service)")
        self.acpi_path = self.method

    @staticmethod
    def _reply_value(out: str) -> int:
        out = out.strip()
        if out.startswith("{"):
            parts = [int(x, 0) for x in out.strip("{}").replace(",", " ").split()]
            parts += [0] * (4 - len(parts))
            return parts[0] | parts[1] << 8 | parts[2] << 16 | parts[3] << 24
        return int(out, 0)

    def _evaluate(self, addr: int, data: int, op: int) -> int:
        buf = bytes([addr & 0xFF, addr >> 8 & 0xFF, data & 0xFF, 0, 0, op, 0, 0]).hex()
        last = None
        for _ in range(3):
            try:
                val = self._reply_value(self._call(f"{self.method} 0x0 0x4 b{buf}"))
            except ValueError as exc:
                raise ECError(f"unexpected WMI reply for {addr:#06x}") from exc
            if val != self.FAILED:
                return val
            last = val
            time.sleep(0.015)
        raise ECError(f"EC did not answer at {addr:#06x} ({last:#x})")

    def _read(self, addr: int) -> int:
        return self._evaluate(addr, 0, self.READ) & 0xFF

    def _write(self, addr: int, value: int) -> None:
        self._evaluate(addr, value, self.WRITE)


class MockEC(BaseEC):
    """In-memory EC, optionally persisted to a JSON file.  Used for tests,
    ``--dry-run`` and for developing the UI on machines without the hardware."""

    name = "mock"

    def __init__(self, path: str | None = None, preset: dict[int, int] | None = None,
                 max_addr: int = C.EC_MAX_ADDR) -> None:
        super().__init__()
        self.max_addr = max_addr
        self.path = path
        self.mem: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []
        if path and os.path.exists(path):
            with open(path) as fh:
                self.mem = {int(k, 0): v for k, v in json.load(fh).items()}
        elif preset is not None:
            self.mem = dict(preset)
        else:
            self.mem = default_mock_registers()

    def _read(self, addr: int) -> int:
        return self.mem.get(addr, 0)

    def _write(self, addr: int, value: int) -> None:
        self.mem[addr] = value
        self.writes.append((addr, value))
        if self.path:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({f"{k:#06x}": v for k, v in sorted(self.mem.items())}, fh, indent=0)
            os.replace(tmp, self.path)


def default_mock_registers() -> dict[int, int]:
    """A plausible 2020-era Uniwill gaming laptop with RGB keyboard, two fans
    and universal fan control."""
    m = {
        C.CPU_TEMP: 52,
        C.GPU_TEMP: 45,
        C.SYSTEM_ID: 0x80,
        C.MAIN_FAN_RPM_HI: 0x0B,
        C.MAIN_FAN_RPM_LO: 0xB8,  # 3000 rpm
        C.SECOND_FAN_RPM_HI: 0x0A,
        C.SECOND_FAN_RPM_LO: 0x28,  # 2600 rpm
        C.PWM_1: 80,
        C.PWM_2: 70,
        C.SUPPORT_1: 0x80,
        C.SUPPORT_2: 0x04,
        C.SUPPORT_5: 0x30,
        C.FAN_CTRL: C.UNIVERSAL_FAN_CTRL_BIT,
        C.KBD_STATUS: (4 << C.KBD_BRIGHTNESS_SHIFT),
        C.RGB_RED: 50,
        C.RGB_GREEN: 50,
        C.RGB_BLUE: 50,
        C.MANUAL_FAN_CTRL: 0x00,
    }
    for i, ch in enumerate(b"MOCKEC-1.0.0  "):
        m[C.ROMID_START + i] = ch
    return m


def open_ec(dry_run: bool = False, mock_path: str | None = None) -> BaseEC:
    """Open the best available EC backend.  Raises :class:`ECUnavailable`."""
    if dry_run or mock_path or os.environ.get("UWCTL_MOCK_EC"):
        return MockEC(mock_path or os.environ.get("UWCTL_MOCK_EC") or None)
    from . import sysfs

    root = sysfs.root()
    if find_inou_path(root):
        return AcpiCallEC(find_inou_path(root))
    if find_wmi_method(root):
        return WmiAcpiCallEC(find_wmi_method(root))
    raise ECUnavailable(
        "neither the INOU0000 ACPI device nor the Uniwill WMI EC interface was found - "
        "this does not look like a Uniwill/Tongfang laptop"
    )
