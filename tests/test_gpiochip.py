"""Finding the right /dev/gpiochip on different Pi 5 kernels, with a fake lgpio."""

import pytest

from cupboard_door import hardware
from cupboard_door.hardware import HardwareError, find_header_chip


class FakeLgpio:
    def __init__(self, labels, refuse=()):
        self.labels, self.refuse, self.open_handles = labels, set(refuse), set()

    def gpiochip_open(self, n):
        if n in self.refuse:
            raise Exception("'can not open gpiochip'")
        self.open_handles.add(n)
        return n

    def gpio_get_chip_info(self, handle):
        return [0, 54, f"gpiochip{handle}", self.labels[handle]]

    def gpiochip_close(self, handle):
        self.open_handles.discard(handle)


def use_chips(monkeypatch, numbers):
    monkeypatch.setattr(hardware, "_chip_numbers", lambda: list(numbers))


def test_newer_pi5_kernel_rp1_is_chip0(monkeypatch):
    use_chips(monkeypatch, [0, 10, 11, 12, 13])
    fake = FakeLgpio({0: "pinctrl-rp1", 10: "gpio-brcmstb@107d508500", 11: "x", 12: "x", 13: "x"})
    assert find_header_chip(fake) == 0
    assert not fake.open_handles  # every probe was closed again


def test_older_pi5_kernel_rp1_is_chip4(monkeypatch):
    use_chips(monkeypatch, [0, 1, 2, 3, 4])
    labels = {0: "gpio-brcmstb@107d508500", 1: "gpio-brcmstb@107d508520", 2: "a", 3: "b", 4: "pinctrl-rp1"}
    assert find_header_chip(FakeLgpio(labels)) == 4  # never picks the SoC chip 0


def test_pi4_uses_bcm_chip(monkeypatch):
    use_chips(monkeypatch, [0, 1])
    assert find_header_chip(FakeLgpio({0: "pinctrl-bcm2711", 1: "raspberrypi-exp-gpio"})) == 0


def test_permission_problem_gives_clear_error(monkeypatch):
    use_chips(monkeypatch, [0, 4])
    fake = FakeLgpio({0: "pinctrl-rp1", 4: "pinctrl-rp1"}, refuse={0, 4})
    with pytest.raises(HardwareError) as err:
        find_header_chip(fake)
    text = str(err.value)
    assert "gpiochip0" in text and "can not open" in text
    assert "running as user" in text  # diagnostics included


def test_no_header_chip_is_an_error(monkeypatch):
    use_chips(monkeypatch, [0])
    with pytest.raises(HardwareError):
        find_header_chip(FakeLgpio({0: "gpio-brcmstb@107d508500"}))


class FakeLgpioDevice:
    """Enough of lgpio for gpiozero's LGPIOFactory and LGPIOPin, with only the
    chips that exist on the reporting Pi 5 (gpiochip11 to gpiochip15)."""

    error = Exception
    SET_PULL_NONE, SET_PULL_UP, SET_PULL_DOWN, BOTH_EDGES = 0, 32, 64, 3

    def __init__(self, chips=(11, 12, 13, 14, 15)):
        self.chips, self.opened, self.levels, self.outputs = set(chips), [], {}, set()

    def gpiochip_open(self, chip):
        if chip not in self.chips:
            raise Exception("'can not open gpiochip'")
        self.opened.append(chip)
        return chip

    def gpiochip_close(self, handle): pass
    def gpio_claim_input(self, h, gpio, flags=0):
        self.outputs.discard(gpio)
        self.levels.setdefault(gpio, 1)

    def gpio_claim_output(self, h, gpio, level=0, flags=0):
        self.outputs.add(gpio)
        self.levels[gpio] = level

    def gpio_write(self, h, gpio, level): self.levels[gpio] = level
    def gpio_read(self, h, gpio): return self.levels.get(gpio, 1)
    def gpio_get_mode(self, h, gpio): return 2 if gpio in self.outputs else 0
    def gpio_free(self, h, gpio): pass

    def __getattr__(self, name):  # any other lgpio call gpiozero might make
        return lambda *a, **k: 0


def test_gpiozero_201_chip_bug_is_worked_around(monkeypatch):
    """gpiozero 2.0.1 ignores chip= and opens gpiochip4 on a Pi 5. Our factory
    must still end up on the chip we asked for."""
    import sys

    fake = FakeLgpioDevice()
    monkeypatch.setitem(sys.modules, "lgpio", fake)
    lgpio_pins = pytest.importorskip("gpiozero.pins.lgpio")
    monkeypatch.setattr(lgpio_pins, "lgpio", fake)
    monkeypatch.setattr(lgpio_pins.LGPIOFactory, "_get_revision", lambda self: 0xD04170)  # Pi 5

    factory = hardware.make_real_factory(15)
    assert factory._chip == 15
    assert 15 in fake.opened

    from cupboard_door.config import DEFAULT_PINS

    hw = hardware.Hardware(DEFAULT_PINS, factory)
    try:
        assert fake.levels[DEFAULT_PINS["en"]] == 1  # driver off from the start
        hw.enable_driver()
        assert fake.levels[DEFAULT_PINS["en"]] == 0
    finally:
        hw.close()
