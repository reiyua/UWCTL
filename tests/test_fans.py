import pytest

from uwctl import constants as C
from uwctl.fans import (PRESETS, SAFETY_TEMP, FanController, FanError, curve_to_table,
                        curve_value, normalize_curve, table_value)


def test_normalize_sorts_and_makes_monotonic():
    pts = normalize_curve([(70, 50), (40, 30), (60, 20)])
    assert pts == [(40, 30), (60, 30), (70, 50), (SAFETY_TEMP, 100)]


def test_safety_point_forced():
    assert normalize_curve([(50, 30)])[-1] == (SAFETY_TEMP, 100)
    # a point beyond the safety temperature is pulled back to it
    pts = normalize_curve([(50, 30), (100, 100)])
    assert pts[-1] == (SAFETY_TEMP, 100)
    assert all(t <= SAFETY_TEMP for t, _ in pts)


@pytest.mark.parametrize("bad", [[], [(150, 50)], [(50, 120)], [(50, -1)]])
def test_normalize_rejects_bad(bad):
    with pytest.raises(FanError):
        normalize_curve(bad)


def test_too_many_points():
    with pytest.raises(FanError):
        normalize_curve([(t, t - 20) for t in range(30, 90, 4)])


@pytest.mark.parametrize("name", list(PRESETS))
def test_table_shape_and_conservative(name):
    curve = PRESETS[name]
    table = curve_to_table(curve)
    assert len(table) == C.FAN_TABLE_LEN
    for start, end, duty in table:
        assert 0 <= start < end <= 0xFF
        assert 1 <= duty <= C.FAN_SPEED_MAX
    # zones are contiguous and cover 0..115
    real = [z for z in table if z[0] < 115]
    assert real[0][0] == 0
    for a, b in zip(real, real[1:]):
        assert a[1] == b[0]
    assert real[-1][1] == 115
    # never slower than the curve (within the 25 % minimum-speed rounding)
    norm = normalize_curve(curve)
    for t in range(0, 115):
        want = curve_value(norm, t)
        got = table_value(table, t) * 100 / C.FAN_SPEED_MAX
        if want >= 12.5:
            assert got + 0.01 >= min(want, 100) - 0.5, (t, want, got)
    # 100 % from the safety temperature upwards
    for t in range(SAFETY_TEMP, 116):
        assert table_value(table, t) == C.FAN_SPEED_MAX
    # monotonic
    duties = [z[2] for z in real]
    assert duties == sorted(duties)


def test_zero_speed_written_as_one():
    table = curve_to_table([(60, 0), (70, 50)])
    assert table[0][2] == 1


def test_min_speed_rounding():
    table = curve_to_table([(30, 20), (89, 20)])
    # 20 % rounds up to the 25 % minimum
    assert table[0][2] == 50


def _fan(ec):
    return FanController(ec)


def test_set_custom_writes_tables(mock_ec):
    fc = _fan(mock_ec)
    cpu, gpu = fc.set_custom(PRESETS["balanced"], PRESETS["silent"])
    m = mock_ec.mem
    for i, (start, end, duty) in enumerate(cpu):
        assert m[C.CPU_TEMP_END_TABLE + i] == end
        assert m[C.CPU_TEMP_START_TABLE + i] == start
        assert m[C.CPU_FAN_SPEED_TABLE + i] == duty
    for i, (start, end, duty) in enumerate(gpu):
        assert m[C.GPU_FAN_SPEED_TABLE + i] == duty
    assert m[C.UNIVERSAL_FAN_CTRL] & C.SPLIT_TABLES_BIT
    assert m[C.AP_OEM_6] & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT
    assert not m[C.MANUAL_FAN_CTRL] & C.FAN_MODE_BOOST
    assert fc.current_mode() == "custom"
    assert fc.custom_active(cpu)


def test_max_and_auto(mock_ec):
    fc = _fan(mock_ec)
    fc.set_custom(PRESETS["balanced"])
    fc.set_max()
    assert fc.current_mode() == "max"
    assert not mock_ec.mem[C.AP_OEM_6] & C.ENABLE_UNIVERSAL_FAN_CTRL_BIT
    fc.set_auto()
    assert fc.current_mode() == "auto"
    assert not mock_ec.mem[C.UNIVERSAL_FAN_CTRL] & C.SPLIT_TABLES_BIT


def test_custom_refused_without_universal(mock_ec):
    mock_ec.mem[C.FAN_CTRL] = 0
    fc = _fan(mock_ec)
    assert fc.capabilities()["modes"] == ["auto", "max"]
    with pytest.raises(FanError):
        fc.set_custom(PRESETS["balanced"])


def test_sensors_from_ec(mock_ec):
    s = _fan(mock_ec).sensors()
    assert s["cpu_temp"] == 52 and s["fan1_rpm"] == 3000 and s["fan2_rpm"] == 2600
    assert s["fan1_duty"] == 40


def test_sensors_from_hwmon(fake_sys):
    h = "class/hwmon/hwmon3/"
    fake_sys.add(h + "name", "uniwill\n")
    fake_sys.add(h + "temp1_input", "61000\n")
    fake_sys.add(h + "temp2_input", "48000\n")
    fake_sys.add(h + "fan1_input", "4100\n")
    fake_sys.add(h + "pwm1", "128\n")
    s = FanController(None).sensors()
    assert s["source"] == "hwmon" and s["cpu_temp"] == 61 and s["fan1_rpm"] == 4100
    assert s["fan1_duty"] == 50
