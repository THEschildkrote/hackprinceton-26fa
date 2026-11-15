import asyncio

import pytest

from nudge.actuators import MIN_SEPARATION_S, MockDriver
from nudge.config import ActuatorConfig, PodConfig


class FakeTime:
    """Deterministic clock: sleeping advances time instantly."""

    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, seconds):
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)

    def blocking_sleep(self, seconds):
        self.t += max(seconds, 0.0)


def make(cfg=None):
    ft = FakeTime()
    driver = MockDriver(cfg or ActuatorConfig(), sleep=ft.sleep, clock=ft.clock, blocking_sleep=ft.blocking_sleep)
    return driver, ft


def assert_never_together(writes):
    for i, (t1, ch1, _) in enumerate(writes):
        for t2, ch2, _ in writes[i + 1:]:
            if ch1 != ch2:
                assert abs(t2 - t1) >= MIN_SEPARATION_S - 1e-9, (t1, ch1, t2, ch2)


def last_angles(writes):
    return {ch: angle for _, ch, angle in writes}


def test_tap_goes_to_contact_then_rest():
    driver, _ = make()
    assert asyncio.run(driver.tap("left")) is True
    assert [(ch, a) for _, ch, a in driver.writes] == [(0, 50.0), (0, 90.0)]


def test_right_tap_uses_channel_one():
    driver, _ = make()
    asyncio.run(driver.tap("right"))
    assert [(ch, a) for _, ch, a in driver.writes] == [(1, 130.0), (1, 90.0)]


def test_double_tap_is_two_strikes():
    driver, _ = make()
    asyncio.run(driver.double_tap("right"))
    assert [a for _, _, a in driver.writes] == [130.0, 90.0, 130.0, 90.0]


def test_stop_hits_both_sides_but_never_together():
    driver, _ = make()
    asyncio.run(driver.stop())
    contacts = [(ch, a) for _, ch, a in driver.writes[:2]]
    assert contacts == [(0, 50.0), (1, 130.0)]
    assert last_angles(driver.writes) == {0: 90.0, 1: 90.0}
    assert_never_together(driver.writes)


def test_arrived_alternates_three_times():
    driver, _ = make()
    asyncio.run(driver.arrived())
    contacts = [ch for _, ch, a in driver.writes if a != 90.0]
    assert contacts == [0, 1, 0, 1, 0, 1]
    assert_never_together(driver.writes)


def test_separation_holds_even_with_zero_timings():
    cfg = ActuatorConfig(hold_s=0.0, settle_s=0.0, double_gap_s=0.0, min_interval_s=0.0)
    driver, _ = make(cfg)

    async def run():
        await driver.arrived()
        await driver.stop()
        await driver.tap("left")
        await driver.tap("right")
        await driver.close()

    asyncio.run(run())
    assert_never_together(driver.writes)


def test_angles_are_clamped():
    cfg = ActuatorConfig(angle_min_deg=20.0, angle_max_deg=160.0,
                         left=PodConfig(0, -40.0, 300.0))
    driver, _ = make(cfg)
    asyncio.run(driver.tap("left"))
    assert [a for _, _, a in driver.writes] == [160.0, 20.0]


def test_steering_taps_are_rate_limited():
    driver, ft = make()

    async def run():
        first = await driver.tap("left")
        second = await driver.tap("right")  # too soon after the first started
        ft.t += 1.0
        third = await driver.tap("right")
        return first, second, third

    assert asyncio.run(run()) == (True, False, True)
    assert driver.dropped == 1


def test_stop_is_never_dropped():
    driver, _ = make()

    async def run():
        await driver.tap("left")
        return await driver.stop()

    assert asyncio.run(run()) is True


def test_returns_to_rest_when_a_write_fails():
    driver, _ = make()
    real_write = driver._write
    calls = {"n": 0}

    def flaky(channel, angle):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("i2c bus error")
        real_write(channel, angle)

    driver._write = flaky
    with pytest.raises(OSError):
        asyncio.run(driver.stop())
    assert last_angles(driver.writes) == {0: 90.0, 1: 90.0}
    assert_never_together(driver.writes)


def test_returns_to_rest_when_cancelled_mid_tap():
    cfg = ActuatorConfig(hold_s=30.0)
    driver = MockDriver(cfg)  # real clock: the tap is parked in its hold

    async def run():
        task = asyncio.create_task(driver.tap("left"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert last_angles(driver.writes) == {0: 90.0, 1: 90.0}


def test_close_rests_both_and_blocks_further_taps():
    driver, _ = make()

    async def run():
        await driver.tap("left")
        await driver.close()
        return await driver.tap("left")

    assert asyncio.run(run()) is False
    assert last_angles(driver.writes) == {0: 90.0, 1: 90.0}


def test_bad_side_is_rejected():
    driver, _ = make()
    with pytest.raises(ValueError):
        asyncio.run(driver.tap("middle"))


# -- the built-in PCA9685 driver ---------------------------------------------------------


class FakeI2c:
    """Stands in for /dev/i2c-1: keeps the chip's registers and every write."""

    def __init__(self):
        self.registers = {}
        self.writes = []
        self.pointer = 0
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        self.pointer = data[0]
        for offset, value in enumerate(data[1:]):
            self.registers[data[0] + offset] = value

    def read(self, count):
        return bytes(self.registers.get(self.pointer + i, 0) for i in range(count))

    def close(self):
        self.closed = True


def test_pca9685_is_set_to_50_hz_the_way_the_datasheet_requires():
    from nudge.actuators import Pca9685

    i2c = FakeI2c()
    Pca9685(i2c, 50, sleep=lambda s: None)
    assert i2c.writes == [
        bytes([0x00, 0x10]),      # MODE1: sleep, so the prescaler can be changed
        bytes([0xFE, 121]),       # PRESCALE: 25 MHz / (4096 x 50 Hz) - 1
        bytes([0x00, 0x20]),      # MODE1: wake, register auto-increment
        bytes([0x00, 0xA0]),      # MODE1: restart the outputs
    ]


def test_pca9685_pulse_widths_become_counts_on_the_right_channel():
    from nudge.actuators import Pca9685

    i2c = FakeI2c()
    chip = Pca9685(i2c, 50, sleep=lambda s: None)
    chip.set_pulse_us(0, 1450)                    # 1450 us of a 20 ms period = 297 of 4096 counts
    assert i2c.writes[-1] == bytes([0x06, 0, 0, 297 & 0xFF, 297 >> 8])
    chip.set_pulse_us(1, 2400)
    assert i2c.writes[-1] == bytes([0x0A, 0, 0, 492 & 0xFF, 492 >> 8])
    assert chip.pulse_us(0) == pytest.approx(1450, abs=3) and chip.pulse_us(1) == pytest.approx(2400, abs=3)
    assert chip.counts(-5) == 0 and chip.counts(10**9) == 4095


def test_builtin_driver_turns_angles_into_pulses_and_rests_on_start():
    from nudge.actuators import Pca9685Driver

    ft = FakeTime()
    i2c = FakeI2c()
    driver = Pca9685Driver(ActuatorConfig(), device=i2c, sleep=ft.sleep, clock=ft.clock,
                           blocking_sleep=ft.blocking_sleep)
    # 0 deg = 500 us, 180 deg = 2400 us, so rest (90 deg) is 1450 us on both channels.
    assert driver.chip.pulse_us(0) == pytest.approx(1450, abs=3)
    assert driver.chip.pulse_us(1) == pytest.approx(1450, abs=3)
    assert driver.pulse_for(50) == pytest.approx(500 + 1900 * 50 / 180)

    seen = []
    real = driver.chip.set_pulse_us
    driver.chip.set_pulse_us = lambda channel, pulse: (seen.append((channel, round(pulse))), real(channel, pulse))
    asyncio.run(driver.tap("left"))
    assert seen == [(0, 1028), (0, 1450)]         # left pod: contact at 50 deg, back to rest
    seen.clear()
    asyncio.run(driver.stop())
    assert seen == [(0, 1028), (1, 1872), (0, 1450), (1, 1450)]


def test_servo_means_the_builtin_driver_when_the_adafruit_library_is_absent(monkeypatch):
    import nudge.actuators as actuators

    made = []
    monkeypatch.setattr(actuators.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(actuators, "Pca9685Driver", lambda cfg: made.append("builtin") or "builtin")
    monkeypatch.setattr(actuators, "ServoDriver", lambda cfg: made.append("servokit") or "servokit")
    assert actuators.make_driver(ActuatorConfig(), "servo") == "builtin"
    monkeypatch.setattr(actuators.importlib.util, "find_spec", lambda name: object())
    assert actuators.make_driver(ActuatorConfig(), "servo") == "servokit"
    assert actuators.make_driver(ActuatorConfig(), "pca9685") == "builtin"
    assert made == ["builtin", "servokit", "builtin"]


def test_builtin_driver_says_what_is_wrong_when_there_is_no_i2c_bus(monkeypatch):
    import nudge.actuators as actuators

    def no_bus(bus, address):
        raise FileNotFoundError(2, "No such file or directory", "/dev/i2c-1")

    monkeypatch.setattr(actuators, "I2cDevice", no_bus)
    with pytest.raises(actuators.DriverUnavailable, match="i2cdetect -y 1"):
        actuators.Pca9685Driver(ActuatorConfig())
