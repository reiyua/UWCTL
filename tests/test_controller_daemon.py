import json
import os
import stat
import threading
import time

import pytest

from uwctl import constants as C
from uwctl.cli import main as cli_main
from uwctl.cli import parse_curve
from uwctl.client import Client, ClientError
from uwctl.controller import Controller
from uwctl.daemon import Daemon
from uwctl.effects import SoftwareEffect
from uwctl.power import Lightbar, Power, PowerError
from uwctl.state import StateError, StateStore, validate


def make_ctl(state_path, ec):
    return Controller(StateStore(state_path), ec=ec)


# ------------------------------------------------------------------ state
def test_validate_defaults_and_errors():
    s = validate({})
    assert s["fans"]["mode"] == "auto" and s["keyboard"]["effect"] == "static"
    with pytest.raises(StateError):
        validate({"keyboard": {"effect": "disco"}})
    with pytest.raises(StateError):
        validate({"keyboard": {"colors": ["#zzzzzz"]}})
    with pytest.raises(StateError):
        validate({"fans": {"cpu_curve": [[200, 50]]}})
    with pytest.raises(StateError):
        validate({"battery": {"charge_limit": 10}})
    assert validate({"keyboard": {"colors": "red"}})["keyboard"]["colors"] == ["#ff0000"]


def test_corrupt_state_file(state_path):
    with open(state_path, "w") as fh:
        fh.write("{not json")
    st = StateStore(state_path)
    assert st.state["fans"]["mode"] == "auto"
    assert os.path.exists(state_path + ".bad")


# ------------------------------------------------------------------ controller
def test_controller_apply_and_update(state_path, mock_ec):
    ctl = make_ctl(state_path, mock_ec)
    errs = ctl.update({"keyboard": {"effect": "static", "colors": ["#ff0000"], "brightness": 50},
                       "fans": {"mode": "custom"}, "profile": {"ac": "turbo"}})
    assert errs == {}
    m = mock_ec.mem
    assert (m[C.RGB_RED], m[C.RGB_GREEN], m[C.RGB_BLUE]) == (50, 0, 0)
    assert (m[C.KBD_STATUS] & C.KBD_BRIGHTNESS_MASK) >> 5 == 2
    assert m[C.AP_OEM_6] & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT
    assert m[C.MANUAL_FAN_CTRL] & C.PROFILE_MASK == 0x10
    assert json.load(open(state_path))["profile"]["ac"] == "turbo"
    assert ctl.status()["fan_mode"] == "custom"


def test_software_effect_runs(state_path, mock_ec):
    ctl = make_ctl(state_path, mock_ec)
    ctl.update({"keyboard": {"effect": "breathing", "colors": ["#00ff00"], "speed": 10}})
    assert ctl.effect is not None
    mock_ec.writes.clear()
    ctl.effect_t0 -= 0.5  # half a period at speed 10 = full brightness
    ctl.render_effect()
    assert (C.RGB_GREEN, 50) in mock_ec.writes


def test_watchdog_reapplies_and_failsafe(state_path, mock_ec):
    ctl = make_ctl(state_path, mock_ec)
    ctl.update({"fans": {"mode": "custom"}})
    # EC forgets the tables (e.g. after resume)
    mock_ec.mem[C.AP_OEM_6] = 0
    ctl.watchdog()
    assert mock_ec.mem[C.AP_OEM_6] & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT
    # runaway temperature -> max fans
    mock_ec.mem[C.CPU_TEMP] = 97
    ctl.watchdog()
    assert ctl.failsafe_active and mock_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    mock_ec.mem[C.CPU_TEMP] = 60
    ctl.watchdog()
    assert not ctl.failsafe_active
    assert not mock_ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    assert ctl.fans.current_mode() == "custom"


def test_battery_brightness_follows_ac(state_path, mock_ec, fake_sys):
    fake_sys.add("class/power_supply/ACAD/type", "Mains\n")
    online = fake_sys.add("class/power_supply/ACAD/online", "1\n")
    ctl = make_ctl(state_path, mock_ec)
    ctl.update({"keyboard": {"brightness": 100, "battery_brightness": 25}})
    assert (mock_ec.mem[C.KBD_STATUS] >> 5) == 4
    with open(online, "w") as fh:
        fh.write("0\n")
    ctl.watchdog()
    assert (mock_ec.mem[C.KBD_STATUS] >> 5) == 1


# ------------------------------------------------------------------ power
def test_power_sysfs_features(fake_sys, mock_ec):
    fake_sys.add("class/power_supply/BAT0/charge_control_end_threshold", "100\n")
    plat = "bus/platform/devices/INOU0000:00/"
    fake_sys.add(plat + "driver/.keep", "")
    fake_sys.add(plat + "fn_lock", "0\n")
    fake_sys.add(plat + "super_key_enable", "1\n")
    p = Power(mock_ec)
    p.set_charge_limit(80)
    assert fake_sys.read("class/power_supply/BAT0/charge_control_end_threshold") == "80"
    with pytest.raises(PowerError):
        p.set_charge_limit(20)
    assert p.toggles() == {"fn_lock": False, "super_key": True}
    p.set_toggle("super_key", False)
    assert fake_sys.read(plat + "super_key_enable") == "0"
    with pytest.raises(PowerError):
        p.set_toggle("touchpad_toggle", True)


def test_platform_profile_fallback(fake_sys):
    fake_sys.add("firmware/acpi/platform_profile", "balanced\n")
    fake_sys.add("firmware/acpi/platform_profile_choices", "low-power balanced performance\n")
    p = Power(None)
    assert p.profile_backend() == "platform_profile" and p.get_profile() == "gaming"
    p.set_profile("office")
    assert fake_sys.read("firmware/acpi/platform_profile") == "low-power"


def test_lightbar_ec(mock_ec):
    mock_ec.mem[C.SUPPORT_1] |= 0x40
    lb = Lightbar(mock_ec)
    assert lb.backend() == "ec"
    lb.set(True, (255, 0, 0), rainbow=True)
    assert mock_ec.mem[C.LIGHTBAR_AC_RGB[0]] == 36
    assert mock_ec.mem[C.LIGHTBAR_AC_CTRL] & C.LIGHTBAR_WELCOME
    lb.set(False)
    assert mock_ec.mem[C.LIGHTBAR_BAT_CTRL] & C.LIGHTBAR_S0_OFF


def test_effect_frames():
    e = SoftwareEffect("rainbow_wave", [], 5, 4, "right")
    frame, mult = e.frame(0.3)
    assert len(frame) == 4 and len(set(frame)) == 4 and mult == 1.0
    b = SoftwareEffect("breathing", [(200, 100, 0)], 1, 1)
    assert b.frame(0)[0][0] == (0, 0, 0)
    assert b.frame(b.period / 2)[0][0] == (200, 100, 0)


# ------------------------------------------------------------------ daemon
@pytest.fixture
def daemon(tmp_path, state_path, mock_ec):
    sock = str(tmp_path / "run" / "uwctl.sock")
    d = Daemon(make_ctl(state_path, mock_ec), sock, group="no-such-group-xyz",
               sessions_dir=str(tmp_path / "no-sessions"))
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.02)
    yield d, sock, mock_ec
    d.stop.set()
    t.join(timeout=5)


def test_daemon_roundtrip(daemon):
    d, sock, ec = daemon
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600  # group missing -> root only
    c = Client(sock)
    assert c.call("ping")["version"]
    caps = c.call("capabilities")
    assert "custom" in caps["fans"]["modes"]
    res = c.call("set", state={"fans": {"mode": "max"}})
    assert res["errors"] == {} and ec.mem[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    assert c.call("status")["fan_mode"] == "max"
    with pytest.raises(ClientError):
        c.call("set", state={"fans": {"mode": "warp"}})
    with pytest.raises(ClientError):
        c.call("format_disk")
    assert "tips" in c.call("doctor")


# ------------------------------------------------------------------ CLI
def test_parse_curve():
    assert parse_curve("40:0, 50:25%,90:100") == [[40, 0], [50, 25], [90, 100]]


def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cli_main(["--dry-run", "fan", "custom", "--cpu", "45:0,60:40,80:90"]) == 0
    assert cli_main(["--dry-run", "kbd", "effect", "wave", "-c", "blue", "-s", "8"]) == 0
    assert cli_main(["--dry-run", "status"]) == 0
    out = capsys.readouterr().out
    assert "Fan mode     custom" in out and "wave #0000ff" in out
    assert cli_main(["--dry-run", "kbd", "color", "notacolour"]) == 1
