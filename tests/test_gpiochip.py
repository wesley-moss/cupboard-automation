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
