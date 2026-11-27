import pytest

from uwctl import constants as C
from uwctl.ec import AcpiCallEC, ECError, MockEC, find_inou_path
from uwctl.keyboard import (ECKeyboard, Ite829xKeyboard, LedClassKeyboard, _hidiocsfeature,
                            find_ite829x, open_keyboard, spread)


# ---------------------------------------------------------------- EC layer
def test_write_allowlist(mock_ec):
    with pytest.raises(ECError):
        mock_ec.write(C.CPU_TEMP, 10)          # sensor - never writable
    with pytest.raises(ECError):
        mock_ec.write(0x0700, 1)               # random register
    with pytest.raises(ECError):
        mock_ec.read(0x1804)                   # beyond ECRR range
    mock_ec.write(C.RGB_RED, 10)
    assert mock_ec.read(C.RGB_RED) == 10


def test_update_bits(mock_ec):
    mock_ec.mem[C.MANUAL_FAN_CTRL] = 0x03
    mock_ec.update_bits(C.MANUAL_FAN_CTRL, C.PROFILE_MASK, 0xA0)
    assert mock_ec.mem[C.MANUAL_FAN_CTRL] == 0xA3


def test_mock_persists(tmp_path):
    p = str(tmp_path / "ec.json")
    a = MockEC(p)
    a.write(C.RGB_BLUE, 33)
    assert MockEC(p).read(C.RGB_BLUE) == 33


def test_find_inou_path(fake_sys):
    fake_sys.add("bus/acpi/devices/INOU0000:00/path", "\\_SB_.INOU\n")
    assert find_inou_path(fake_sys.path) == "\\_SB_.INOU"


class FakeAcpi(AcpiCallEC):
    def __init__(self, replies):
        self._lock = __import__("threading").RLock()
        self.acpi_path = "\\_SB_.INOU"
        self.replies = replies
        self.calls = []

    def _call(self, expr):
        self.calls.append(expr)
        out = self.replies.pop(0)
        if out.startswith("Error"):
            raise ECError(out)
        return out


def test_acpi_call_protocol():
    ec = FakeAcpi(["0x5a", "0x0", "Error: AE_NOT_FOUND", "0xfefefefe"])
    assert ec.read(0x043E) == 0x5A
    assert ec.calls[-1] == "\\_SB_.INOU.ECRR 0x43e"
    ec.write(C.RGB_RED, 0x20)
    assert ec.calls[-1] == "\\_SB_.INOU.ECRW 0x769 0x20"
    with pytest.raises(ECError):
        ec.read(0x0740)
    with pytest.raises(ECError):
        ec.read(0x0740)  # 0xfefefefe = EC communication failure


# ---------------------------------------------------------------- EC keyboard
def test_ec_keyboard_colour_and_brightness(mock_ec):
    kb = ECKeyboard(mock_ec, levels=4)
    assert kb.kind == "rgb"
    kb.set_colors([(255, 128, 0)], 0.5)
    m = mock_ec.mem
    assert (m[C.RGB_RED], m[C.RGB_GREEN], m[C.RGB_BLUE]) == (50, 25, 0)
    assert m[C.TRIGGER] & C.RGB_APPLY_COLOR
    assert (m[C.KBD_STATUS] & C.KBD_BRIGHTNESS_MASK) >> 5 == 2
    assert m[C.KBD_STATUS] & C.KBD_APPLY
    kb.set_colors([(0, 0, 0)], 1.0)  # black would make the EC restore defaults
    assert (m[C.RGB_RED], m[C.RGB_GREEN], m[C.RGB_BLUE]) == (1, 1, 1)
    kb.off()
    assert (m[C.KBD_STATUS] & C.KBD_BRIGHTNESS_MASK) == 0


def test_ec_keyboard_white(mock_ec):
    mock_ec.mem[C.KBD_STATUS] = C.KBD_WHITE_ONLY
    kb = ECKeyboard(mock_ec, levels=2)
    assert kb.kind == "white" and not kb.is_rgb
    kb.set_colors([(255, 0, 0)], 1.0)
    assert C.RGB_RED not in [a for a, _ in mock_ec.writes]
    assert (mock_ec.mem[C.KBD_STATUS] & C.KBD_BRIGHTNESS_MASK) >> 5 == 2
    assert kb.effects() == ["static", "breathing", "off"]


# ---------------------------------------------------------------- LED class
def _led(fs, name, maxb=4, multi=True, mmi=None):
    d = f"class/leds/{name}/"
    fs.add(d + "brightness", "0")
    fs.add(d + "max_brightness", str(maxb))
    if multi:
        fs.add(d + "multi_index", "red green blue")
        fs.add(d + "multi_intensity", "0 0 0")
    if mmi:
        fs.add(d + "multi_max_intensity", mmi)
    return d


def test_led_class_uniwill(fake_sys):
    d = _led(fake_sys, "uniwill:rgb:kbd_backlight")
    _led(fake_sys, "uniwill:rgb:status")  # lightbar must not be picked up
    leds = LedClassKeyboard.discover()
    assert len(leds) == 1
    kb = LedClassKeyboard(leds)
    kb.set_colors([(255, 0, 51)], 0.5)
    assert fake_sys.read(d + "multi_intensity") == "50 0 10"
    assert fake_sys.read(d + "brightness") == "2"


def test_led_class_multizone_order(fake_sys):
    for n in ("rgb:kbd_backlight", "rgb:kbd_backlight_1", "rgb:kbd_backlight_2", "rgb:kbd_backlight_10"):
        _led(fake_sys, n, maxb=255, mmi="255 255 255")
    kb = LedClassKeyboard(LedClassKeyboard.discover())
    assert kb.kind == "multizone" and kb.zones == 4
    assert [l.rsplit("/", 1)[1] for l in kb.leds][-1] == "rgb:kbd_backlight_10"
    kb.set_colors([(255, 0, 0), (0, 0, 255)], 1.0)
    assert fake_sys.read("class/leds/rgb:kbd_backlight/multi_intensity") == "255 0 0"
    assert fake_sys.read("class/leds/rgb:kbd_backlight_10/multi_intensity") == "0 0 255"


def test_spread():
    assert spread([(1, 1, 1)], 3) == [(1, 1, 1)] * 3
    assert spread([(1, 0, 0), (0, 1, 0)], 4) == [(1, 0, 0), (1, 0, 0), (0, 1, 0), (0, 1, 0)]


# ---------------------------------------------------------------- ITE 829x
class FakeHid:
    def __init__(self):
        self.packets = []

    def send_feature(self, data):
        assert len(data) == 6 and data[0] == 0xCC
        self.packets.append(tuple(data[1:]))


def test_ioctl_number():
    assert _hidiocsfeature(6) == 0xC0064806


def test_829x_led_id():
    assert Ite829xKeyboard.led_id(0, 0) == 0
    assert Ite829xKeyboard.led_id(2, 6) == 0x46
    assert Ite829xKeyboard.led_id(5, 19) == 0xB3


def test_829x_static():
    h = FakeHid()
    kb = Ite829xKeyboard(h)
    kb.set_colors([(255, 0, 0)], 1.0)
    assert h.packets[0] == (0x00, 0x0C, 0, 0, 0)        # clear
    assert h.packets[1] == (0x09, 10, 5, 0, 0)         # brightness 10, speed
    leds = h.packets[2:]
    assert len(leds) == 120
    assert all(p[0] == 0x01 and p[2:] == (255, 0, 0) for p in leds)
    assert {p[1] for p in leds} == {Ite829xKeyboard.led_id(r, c) for r in range(6) for c in range(20)}


def test_829x_four_zones():
    h = FakeHid()
    Ite829xKeyboard(h).set_colors([(1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0)], 1.0)
    by_id = {p[1]: p[2] for p in h.packets if p[0] == 0x01}
    assert by_id[Ite829xKeyboard.led_id(0, 0)] == 1
    assert by_id[Ite829xKeyboard.led_id(3, 19)] == 4


def test_829x_effects():
    h = FakeHid()
    kb = Ite829xKeyboard(h)
    kb.set_hw_effect("wave", [(10, 20, 30)], 0.5, 7, "right")
    assert h.packets[:2] == [(0x00, 0x04, 0, 0, 0), (0x15, 0xA1 + 7, 10, 20, 30)]
    assert h.packets[-1] == (0x09, 5, 7, 0, 0)
    h.packets.clear()
    kb.set_hw_effect("rainbow_wave", [], 1.0, 3, "up")
    assert h.packets[1] == (0x15, 0x71 + 4, 0, 0, 0)
    h.packets.clear()
    kb.set_hw_effect("breathing", [(1, 2, 3)], 1.0, 5, "right")
    assert h.packets[0] == (0x0A, 0xAA, 1, 2, 3)
    h.packets.clear()
    kb.set_hw_effect("rainbow", [], 1.0, 5, "right")
    assert h.packets[0] == (0x00, 0x02, 0, 0, 0)
    h.packets.clear()
    kb.set_hw_effect("scan", [(1, 1, 1), (2, 2, 2)], 1.0, 5, "right")
    assert h.packets[:3] == [(0x00, 0x0A, 0, 0, 0), (0x17, 0xA1, 1, 1, 1), (0x17, 0xA2, 2, 2, 2)]
    kb.off()
    assert h.packets[-1] == (0x09, 0, 0, 0, 0)


def test_find_ite829x(fake_sys):
    fake_sys.add("class/hidraw/hidraw0/device/uevent", "HID_ID=0003:0000048D:00008910\n")
    fake_sys.add("class/hidraw/hidraw0/device/report_descriptor", "\x06\x00\xff", "w")
    fake_sys.add("class/hidraw/hidraw1/device/uevent", "HID_ID=0003:0000048D:00008910\n")
    with open(fake_sys.path + "/class/hidraw/hidraw1/device/report_descriptor", "wb") as fh:
        fh.write(b"\x06\x89\xff\x85\xcc\x09\x01")
    fake_sys.add("class/hidraw/hidraw2/device/uevent", "HID_ID=0003:0000046D:0000C52B\n")
    assert find_ite829x() == "/dev/hidraw1"


def test_open_keyboard_prefers_leds_over_ec(fake_sys, mock_ec):
    _led(fake_sys, "uniwill:rgb:kbd_backlight")
    assert open_keyboard(mock_ec).backend == "leds"
    assert open_keyboard(mock_ec, "ec").backend == "ec"


# ---------------------------------------------------------------- WMI EC (2020 Tongfang, no INOU0000)
from uwctl.ec import WmiAcpiCallEC, find_wmi_method, open_ec  # noqa: E402


def _fake_wmi_sys(fake_sys):
    """Lay out /sys like Ray's GK5MR0O: GetSetULong on \\_SB_.AMW0, object id BC."""
    import os
    guid = "ABBC0F6F-8EA1-11D1-00A0-C90629100000-17"
    real = f"devices/platform/PNP0C14:03/wmi_bus/wmi_bus-PNP0C14:03/{guid}"
    fake_sys.add(real + "/object_id", "BC\n")
    fake_sys.add("devices/platform/PNP0C14:01/wmi_bus/wmi_bus-PNP0C14:01/X/object_id", "AA\n")
    os.makedirs(fake_sys.path + "/bus/wmi/devices", exist_ok=True)
    os.symlink(fake_sys.path + "/" + real, fake_sys.path + "/bus/wmi/devices/" + guid)
    os.symlink(fake_sys.path + "/devices/platform/PNP0C14:01/wmi_bus/wmi_bus-PNP0C14:01/X",
               fake_sys.path + "/bus/wmi/devices/DBBC0F6F-8EA1-11D1-00A0-C90629100000-10")
    fake_sys.add("bus/acpi/devices/PNP0C14:03/path", "\\_SB_.AMW0\n")
    fake_sys.add("bus/acpi/devices/PNP0C14:01/path", "\\_SB_.PCI0.WMI1\n")


def test_find_wmi_method(fake_sys):
    _fake_wmi_sys(fake_sys)
    assert find_wmi_method(fake_sys.path) == "\\_SB_.AMW0.WMBC"


class FakeWmi(WmiAcpiCallEC):
    def __init__(self, replies):
        import threading
        self._lock = threading.RLock()
        self.method = self.acpi_path = "\\_SB_.AMW0.WMBC"
        self.replies = list(replies)
        self.calls = []

    def _call(self, expr):
        self.calls.append(expr)
        return self.replies.pop(0)


def test_wmi_protocol():
    ec = FakeWmi(["{0x4e, 0x00, 0x00, 0x00}", "{0x00, 0x00, 0x00, 0x00}",
                  "{0xfe, 0xfe, 0xfe, 0xfe}", "{0x2a, 0x00, 0x00, 0x00}"])
    # read CPU temp (0x043e): addr lo/hi, data 0, op byte 5 = 1 (read)
    assert ec.read(C.CPU_TEMP) == 0x4E
    assert ec.calls[0] == "\\_SB_.AMW0.WMBC 0x0 0x4 b3e04000000010000"
    # write RGB red (0x0769) = 0x20: op byte 5 = 0 (write)
    ec.write(C.RGB_RED, 0x20)
    assert ec.calls[1] == "\\_SB_.AMW0.WMBC 0x0 0x4 b6907200000000000"
    # EC busy once -> retried
    assert ec.read(C.KBD_STATUS) == 0x2A
    assert len(ec.calls) == 4
    # addresses above 0xFFF are reachable over WMI; the direct fan registers are
    # writable, their neighbours are not
    ec.replies += ["{0x10, 0x00, 0x00, 0x00}", "{0x00, 0x00, 0x00, 0x00}"]
    assert ec.read(0x1804) == 0x10
    ec.write(C.PWM_1_DIRECT, 0x40)
    assert ec.calls[-1] == "\\_SB_.AMW0.WMBC 0x0 0x4 b0418400000000000"
    with pytest.raises(ECError):
        ec.write(0x1805, 1)


def test_wmi_gives_up():
    ec = FakeWmi(["{0xfe, 0xfe, 0xfe, 0xfe}"] * 3)
    with pytest.raises(ECError):
        ec.read(C.CPU_TEMP)


def test_open_ec_falls_back_to_wmi(fake_sys, tmp_path, monkeypatch):
    _fake_wmi_sys(fake_sys)
    proc = tmp_path / "call"
    proc.write_text("not called")
    monkeypatch.setattr(WmiAcpiCallEC, "PROC", str(proc))
    ec = open_ec()
    assert ec.name == "wmi" and ec.method == "\\_SB_.AMW0.WMBC"
