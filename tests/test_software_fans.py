"""Software fan curves for ECs without universal fan control (e.g. Ray's
Tongfang GK5MR0O, reached over WMI), plus seat-based socket access."""

import errno
import os

import pytest

from uwctl import constants as C
from uwctl.access import (GROUP_OBJ, MASK, OTHER, USER, USER_OBJ, acl_bytes,
                          local_session_uids, SocketAccess)
from uwctl.controller import Controller
from uwctl.daemon import release_fans
from uwctl.ec import ECError, MockEC, default_mock_registers
from uwctl.fans import PRESETS, FanController, FanError, FanSmoother
from uwctl.state import StateStore


@pytest.fixture
def gk5_ec():
    regs = default_mock_registers()
    regs[C.FAN_CTRL] = 0          # no universal fan control
    regs[C.PROJECT_ID] = 0x0F     # CML gaming
    regs[C.CPU_TEMP], regs[C.GPU_TEMP] = 56, 45
    return MockEC(preset=regs, max_addr=0xFFFF)


def ctl_for(state_path, ec, daemon=True):
    ctl = Controller(StateStore(state_path), ec=ec)
    ctl.daemon_mode = daemon
    return ctl


def direct(ec):
    return ec.mem.get(C.PWM_1_DIRECT), ec.mem.get(C.PWM_2_DIRECT)


# ------------------------------------------------------------------ smoother
def test_smoother_up_fast_down_slow():
    s = FanSmoother(PRESETS["balanced"], hysteresis=3, step_down=5)
    assert s.update(70) == 60
    assert s.update(80) == 80               # straight up
    assert s.update(79) == 80               # within hysteresis: hold
    assert s.update(60) == 75               # big drop: at most 5 % per update
    for _ in range(10):
        v = s.update(60)
    assert v == pytest.approx(46.0)          # settles at curve(60 + 3 °C)
    assert s.update(None) == v               # sensor glitch: keep last value


def test_smoother_first_unknown_is_safe():
    assert FanSmoother(PRESETS["silent"]).update(None) == 100.0


# ------------------------------------------------------------------ capabilities
def test_custom_kind(gk5_ec, mock_ec):
    assert FanController(gk5_ec).capabilities()["custom_kind"] == "software"
    assert "custom" in FanController(gk5_ec).capabilities()["modes"]
    assert FanController(mock_ec).capabilities()["custom_kind"] == "hardware"
    mock_ec.mem[C.FAN_CTRL] = 0     # old EC reached via INOU (no 0x1804 access)
    caps = FanController(mock_ec).capabilities()
    assert caps["custom_kind"] is None and "custom" not in caps["modes"]


def test_direct_registers_need_wmi(mock_ec):
    with pytest.raises(ECError):
        mock_ec.write(C.PWM_1_DIRECT, 50)   # INOU-style EC can't reach 0x1804


# ------------------------------------------------------------------ controller
def test_software_curve_needs_daemon(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec, daemon=False)
    errs = ctl.update({"fans": {"mode": "custom"}})
    assert "uwctld" in errs["fans"]
    assert not gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST


def test_software_curve_engages_and_tracks(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    assert ctl.update({"fans": {"mode": "custom", "cpu_curve": PRESETS["balanced"],
                                "gpu_curve": PRESETS["balanced"]}}) == {}
    m = gk5_ec.mem
    assert m[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    # balanced @ 56 °C = 34 % -> 68/200, both fans together
    assert direct(gk5_ec) == (68, 68)
    # engage writes the duty repeatedly to beat the full-speed ramp
    assert sum(1 for a, v in gk5_ec.writes if a == C.PWM_1_DIRECT) >= 10
    st = ctl.status()
    assert st["fan_mode"] == "custom" and st["fan_control"] == "software"

    m[C.CPU_TEMP] = 75
    ctl.fan_tick()
    assert direct(gk5_ec) == (140, 140)     # 70 % straight away
    m[C.CPU_TEMP] = 60
    ctl.fan_tick()
    assert direct(gk5_ec) == (130, 130)     # only 5 % down per tick
    for _ in range(20):
        ctl.fan_tick()
    assert direct(gk5_ec) == (92, 92)       # settles at curve(63 °C) = 46 %


def test_separate_fans_and_sleeping_dgpu(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    ctl.update({"fans": {"mode": "custom", "same_speed": False,
                         "cpu_curve": [[40, 0], [80, 100]], "gpu_curve": [[40, 0], [60, 100]]}})
    gk5_ec.mem[C.CPU_TEMP], gk5_ec.mem[C.GPU_TEMP] = 60, 55
    ctl.fan_tick()
    assert direct(gk5_ec) == (100, 150)     # CPU 50 %, GPU 75 %
    gk5_ec.mem[C.GPU_TEMP] = 0              # dGPU powered down -> follow CPU
    for _ in range(20):
        ctl.fan_tick()
    assert direct(gk5_ec) == (100, 100)


def test_reengage_when_boost_switched_off(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    ctl.update({"fans": {"mode": "custom"}})
    gk5_ec.mem[C.MANUAL_FAN_CTRL] &= ~C.FAN_MODE_BOOST   # Fn fan key / resume
    ctl.fan_tick()
    assert gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST


def test_software_failsafe(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    ctl.update({"fans": {"mode": "custom", "cpu_curve": PRESETS["silent"]}})
    gk5_ec.mem[C.CPU_TEMP] = 96
    ctl.fan_tick()
    assert ctl.failsafe_active and direct(gk5_ec) == (200, 200)
    gk5_ec.mem[C.CPU_TEMP] = 85               # still too warm to release
    ctl.fan_tick()
    assert direct(gk5_ec) == (200, 200)
    gk5_ec.mem[C.CPU_TEMP] = 60
    ctl.fan_tick()
    assert not ctl.failsafe_active and direct(gk5_ec)[0] < 200


def test_unreadable_temperature_means_full_speed(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    ctl.update({"fans": {"mode": "custom"}})

    def broken(addr):
        if addr == C.CPU_TEMP:
            raise ECError("EC did not answer")
        return gk5_ec.mem.get(addr, 0)

    gk5_ec._read = broken
    ctl.fan_tick()
    assert direct(gk5_ec) == (200, 200)


def test_release_and_modes(state_path, gk5_ec):
    ctl = ctl_for(state_path, gk5_ec)
    ctl.update({"fans": {"mode": "custom"}})
    ctl.release_fans()
    assert ctl.sw is None and not gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    ctl.update({"fans": {"mode": "max"}})
    assert gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST and direct(gk5_ec) == (200, 200)
    ctl.update({"fans": {"mode": "auto"}})
    assert ctl.sw is None and not gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST


def test_release_fans_after_crash(state_path, gk5_ec):
    StateStore(state_path).update({"fans": {"mode": "custom"}})
    gk5_ec.mem[C.MANUAL_FAN_CTRL] |= C.FAN_MODE_BOOST      # daemon died mid-curve
    assert release_fans(state_path, ec=gk5_ec) == 0
    assert not gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    # a deliberate Max setting is left alone
    StateStore(state_path).update({"fans": {"mode": "max"}})
    gk5_ec.mem[C.MANUAL_FAN_CTRL] |= C.FAN_MODE_BOOST
    release_fans(state_path, ec=gk5_ec)
    assert gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST


def test_hardware_ec_still_uses_tables(state_path, mock_ec):
    ctl = ctl_for(state_path, mock_ec)
    ctl.update({"fans": {"mode": "custom"}})
    assert ctl.sw is None and ctl.cpu_table
    with pytest.raises(FanError):
        FanController(MockEC(preset={C.FAN_CTRL: 0})).set_custom(PRESETS["balanced"])


# ------------------------------------------------------------------ socket access
def _session(d, name, **kv):
    with open(os.path.join(d, name), "w") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def test_local_session_uids(tmp_path):
    d = str(tmp_path)
    _session(d, "2", UID=1000, USER="ray", ACTIVE=1, REMOTE=0, SEAT="seat0", CLASS="user")
    _session(d, "c1", UID=964, USER="sddm", ACTIVE=0, SEAT="seat0", CLASS="greeter")
    _session(d, "5", UID=1001, USER="friend", ACTIVE=1, REMOTE=1, CLASS="user")      # ssh
    _session(d, "7", UID=1002, USER="other", ACTIVE=0, REMOTE=0, SEAT="seat0", CLASS="user")
    _session(d, "8", UID=1003, USER="tty", ACTIVE=1, REMOTE=0, SEAT="seat0")
    open(os.path.join(d, "2.ref"), "w").close()
    assert local_session_uids(d) == {1000, 1003}


def test_acl_bytes_layout():
    import struct
    b = acl_bytes({1000}, group_rw=True)
    assert struct.unpack("<I", b[:4])[0] == 2
    entries = [struct.unpack("<HHI", b[i:i + 8]) for i in range(4, len(b), 8)]
    assert [e[0] for e in entries] == [USER_OBJ, USER, GROUP_OBJ, MASK, OTHER]
    assert entries[1] == (USER, 6, 1000) and entries[-1][1] == 0
    assert acl_bytes(set(), group_rw=False)[4 + 8 + 2:4 + 8 + 4] == b"\x00\x00"


def test_socket_access_applies_acl(tmp_path):
    sock = tmp_path / "s"
    sock.write_text("")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _session(str(sessions), "2", UID=1000, ACTIVE=1, REMOTE=0, SEAT="seat0", CLASS="user")
    acc = SocketAccess(str(sock), True, str(sessions))
    acc.refresh()
    if not acc.enabled:
        pytest.skip("filesystem without POSIX ACL support")
    assert os.getxattr(str(sock), "system.posix_acl_access") == acl_bytes({1000}, True)
    os.remove(sessions / "2")                     # user logs out
    acc.refresh()
    with pytest.raises(OSError) as e:
        os.getxattr(str(sock), "system.posix_acl_access")
    assert e.value.errno == errno.ENODATA


def test_daemon_runs_curve_and_releases_on_stop(tmp_path, state_path, gk5_ec):
    import threading
    import time
    from uwctl.client import Client
    from uwctl.daemon import Daemon

    sock = str(tmp_path / "run" / "uwctl.sock")
    d = Daemon(Controller(StateStore(state_path), ec=gk5_ec), sock, group="no-such-group-xyz",
               sessions_dir=str(tmp_path / "no-sessions"))
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.02)
    c = Client(sock)
    assert c.call("set", state={"fans": {"mode": "custom", "cpu_curve": PRESETS["balanced"]}})["errors"] == {}
    assert direct(gk5_ec) == (68, 68)
    gk5_ec.mem[C.CPU_TEMP] = 80
    deadline = time.time() + 6
    while direct(gk5_ec) != (160, 160) and time.time() < deadline:
        time.sleep(0.1)
    assert direct(gk5_ec) == (160, 160)          # the loop picked up the new temperature
    assert c.call("status")["fan_control"] == "software"
    d.stop.set()
    t.join(timeout=5)
    assert not gk5_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST   # fans back on Auto
