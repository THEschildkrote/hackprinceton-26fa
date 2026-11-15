"""Shoulder-tap actuators.

TapDriver implements the tap primitives on top of one low-level operation,
`_write(channel, angle)`. Pca9685Driver sends that to the PCA9685 through
Linux's I2C device with no library; ServoDriver does the same through
adafruit-circuitpython-servokit when that is installed; MockDriver logs it,
so everything above this module runs on a laptop.

Guarantees, for every driver:
  * the two servos are never commanded within 50 ms of each other, so their
    inrush currents do not stack on the Pi's 5 V rail;
  * every angle is clamped to [angle_min_deg, angle_max_deg];
  * both arms go back to rest when a pattern fails, when the driver is
    closed, and when the process exits.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib.util
import logging
import os
import time
from typing import Awaitable, Callable

from .config import ActuatorConfig

log = logging.getLogger("nudge.taps")

SIDES = ("left", "right")
MIN_SEPARATION_S = 0.050


class DriverUnavailable(RuntimeError):
    pass


def other(side: str) -> str:
    return "right" if side == "left" else "left"


class TapDriver:
    def __init__(
        self,
        cfg: ActuatorConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        blocking_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._blocking_sleep = blocking_sleep
        self._pods = {"left": cfg.left, "right": cfg.right}
        self._last_move = {"left": float("-inf"), "right": float("-inf")}
        self._lock = asyncio.Lock()
        self._last_start = float("-inf")
        self._closed = False
        self.dropped = 0

    # -- low level ---------------------------------------------------------

    def _write(self, channel: int, angle: float) -> None:
        raise NotImplementedError

    def clamp(self, angle: float) -> float:
        return min(max(float(angle), self.cfg.angle_min_deg), self.cfg.angle_max_deg)

    async def _move(self, side: str, angle: float) -> None:
        """Command one servo, waiting out the 50 ms window after the other one."""
        wait = self._last_move[other(side)] + MIN_SEPARATION_S - self._clock()
        if wait > 0:
            await self._sleep(wait)
        self._last_move[side] = self._clock()
        self._write(self._pods[side].channel, self.clamp(angle))

    def rest_now(self) -> None:
        """Return both arms to rest without awaiting. Safe to call at any time."""
        # Start with the side that moved last: re-commanding the same servo
        # cannot collide with the other one.
        first = max(SIDES, key=lambda s: self._last_move[s])
        for i, side in enumerate((first, other(first))):
            if i:
                self._blocking_sleep(MIN_SEPARATION_S)
            try:
                self._write(self._pods[side].channel, self.clamp(self._pods[side].rest_deg))
            except Exception:
                log.exception("could not return %s pod to rest", side)
            self._last_move[side] = self._clock()

    # -- patterns ----------------------------------------------------------

    async def _strike(self, side: str) -> None:
        pod = self._pods[side]
        await self._move(side, pod.contact_deg)
        await self._sleep(self.cfg.hold_s)
        await self._move(side, pod.rest_deg)
        await self._sleep(self.cfg.settle_s)

    async def _run(self, label: str, body: Callable[[], Awaitable[None]], *, limited: bool) -> bool:
        """Run one pattern at a time. Steering taps are dropped rather than
        queued when one is already playing or the last one was too recent."""
        if self._closed:
            return False
        if limited and (self._lock.locked() or self._clock() - self._last_start < self.cfg.min_interval_s):
            self.dropped += 1
            log.debug("dropped %s (rate limit)", label)
            return False
        async with self._lock:
            if self._closed:
                return False
            self._last_start = self._clock()
            log.info("%s", label)
            try:
                await body()
            except BaseException:
                self.rest_now()
                raise
        return True

    async def tap(self, side: str) -> bool:
        _check(side)
        return await self._run(f"TAP      {side}", lambda: self._strike(side), limited=True)

    async def double_tap(self, side: str) -> bool:
        _check(side)

        async def body() -> None:
            await self._strike(side)
            await self._sleep(self.cfg.double_gap_s)
            await self._strike(side)

        return await self._run(f"TAP TAP  {side}", body, limited=True)

    async def stop(self) -> bool:
        """Both shoulders together (offset by stagger_s)."""
        left, right = self._pods["left"], self._pods["right"]

        async def body() -> None:
            await self._move("left", left.contact_deg)
            await self._sleep(self.cfg.stagger_s)
            await self._move("right", right.contact_deg)
            await self._sleep(self.cfg.hold_s)
            await self._move("left", left.rest_deg)
            await self._sleep(self.cfg.stagger_s)
            await self._move("right", right.rest_deg)
            await self._sleep(self.cfg.settle_s)

        return await self._run("STOP     both", body, limited=False)

    async def arrived(self) -> bool:
        """Left, right, three times over."""

        async def body() -> None:
            for _ in range(3):
                await self._strike("left")
                await self._strike("right")

        return await self._run("ARRIVED  left-right x3", body, limited=False)

    async def do(self, kind: str, side: str | None = None) -> bool:
        if kind == "tap":
            return await self.tap(side or "")
        if kind == "double_tap":
            return await self.double_tap(side or "")
        if kind == "stop":
            return await self.stop()
        if kind == "arrived":
            return await self.arrived()
        raise ValueError(f"unknown tap pattern: {kind!r}")

    async def jog(self, side: str, angle: float) -> float:
        """Hold one arm at an angle, for finding rest and contact positions."""
        _check(side)
        async with self._lock:
            await self._move(side, angle)
        return self.clamp(angle)

    async def rest(self) -> None:
        async with self._lock:
            for side in SIDES:
                await self._move(side, self._pods[side].rest_deg)

    async def close(self) -> None:
        """Wait for the current pattern, then leave both arms at rest."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.rest()
        except BaseException:
            self.rest_now()
            raise

    async def __aenter__(self) -> "TapDriver":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _check(side: str) -> None:
    if side not in SIDES:
        raise ValueError(f"side must be 'left' or 'right', not {side!r}")


class MockDriver(TapDriver):
    """Logs instead of moving servos. `writes` records (time, channel, angle)."""

    def __init__(self, cfg: ActuatorConfig, **kwargs) -> None:
        super().__init__(cfg, **kwargs)
        self.writes: list[tuple[float, int, float]] = []

    def _write(self, channel: int, angle: float) -> None:
        self.writes.append((self._clock(), channel, angle))
        log.debug("servo ch%d -> %.0f deg", channel, angle)


class ServoDriver(TapDriver):
    """Two SG92R servos on a PCA9685, through adafruit-circuitpython-servokit."""

    def __init__(self, cfg: ActuatorConfig, **kwargs) -> None:
        super().__init__(cfg, **kwargs)
        try:
            from adafruit_servokit import ServoKit
        except ImportError as exc:
            raise DriverUnavailable(
                "adafruit-circuitpython-servokit is not installed. On the Pi run "
                "pip install -e '.[pi]'; on a laptop use --mock."
            ) from exc
        except Exception as exc:  # Blinka refuses to load on boards it does not know
            raise DriverUnavailable(f"the servo library cannot run on this machine ({exc}); use --mock") from exc
        try:
            self._kit = ServoKit(channels=16, address=cfg.i2c_address, frequency=cfg.pwm_freq_hz)
        except Exception as exc:
            raise DriverUnavailable(
                f"no PCA9685 at I2C address 0x{cfg.i2c_address:02x} ({exc}). Check the wiring, "
                "that I2C is enabled (sudo raspi-config nonint do_i2c 0), and `i2cdetect -y 1`."
            ) from exc
        for pod in self._pods.values():
            self._kit.servo[pod.channel].set_pulse_width_range(cfg.min_pulse_us, cfg.max_pulse_us)
        atexit.register(self.rest_now)
        self.rest_now()

    def _write(self, channel: int, angle: float) -> None:
        self._kit.servo[channel].angle = angle


class I2cDevice:
    """One device on Linux's I2C bus, through /dev/i2c-N. Standard library only."""

    I2C_SLAVE = 0x0703  # ioctl: choose the device address for this file descriptor

    def __init__(self, bus: int, address: int) -> None:
        import fcntl  # not on Windows: the caller reports that as "driver unavailable"

        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, self.I2C_SLAVE, address)
        except OSError:
            os.close(self._fd)
            raise

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    def read(self, count: int) -> bytes:
        return os.read(self._fd, count)

    def close(self) -> None:
        os.close(self._fd)


class Pca9685:
    """The PCA9685 PWM chip: set the frequency once, then a pulse width per channel."""

    MODE1, PRESCALE, LED0_ON_L = 0x00, 0xFE, 0x06
    SLEEP, AUTO_INCREMENT, RESTART = 0x10, 0x20, 0x80
    OSCILLATOR_HZ = 25_000_000
    STEPS = 4096  # counts in one PWM period

    def __init__(self, device, freq_hz: int, sleep: Callable[[float], None] = time.sleep) -> None:
        self._device = device
        self.freq_hz = freq_hz
        prescale = round(self.OSCILLATOR_HZ / (self.STEPS * freq_hz)) - 1
        self._set(self.MODE1, self.SLEEP)  # the prescaler can only be changed while asleep
        self._set(self.PRESCALE, prescale)
        self._set(self.MODE1, self.AUTO_INCREMENT)  # wake up
        sleep(0.005)  # the oscillator needs 500 us; be generous
        self._set(self.MODE1, self.RESTART | self.AUTO_INCREMENT)

    def _set(self, register: int, *values: int) -> None:
        self._device.write(bytes([register, *values]))

    def counts(self, pulse_us: float) -> int:
        return min(self.STEPS - 1, max(0, round(pulse_us * self.freq_hz * self.STEPS / 1_000_000)))

    def set_pulse_us(self, channel: int, pulse_us: float) -> None:
        """High from count 0 to the count that makes a pulse this long."""
        off = self.counts(pulse_us)
        self._set(self.LED0_ON_L + 4 * channel, 0, 0, off & 0xFF, off >> 8)

    def pulse_us(self, channel: int) -> float:
        """Read back what the chip is putting out on a channel."""
        self._device.write(bytes([self.LED0_ON_L + 4 * channel + 2]))
        low, high = self._device.read(2)
        return ((high & 0x0F) << 8 | low) * 1_000_000 / (self.freq_hz * self.STEPS)

    def close(self) -> None:
        self._device.close()


class Pca9685Driver(TapDriver):
    """The same two servos with nothing installed: the PCA9685 is driven
    straight through /dev/i2c-N. Used when the Adafruit library is absent."""

    def __init__(self, cfg: ActuatorConfig, device=None, **kwargs) -> None:
        super().__init__(cfg, **kwargs)
        try:
            self.chip = Pca9685(device or I2cDevice(cfg.i2c_bus, cfg.i2c_address), cfg.pwm_freq_hz)
        except ImportError as exc:
            raise DriverUnavailable("the built-in servo driver needs Linux's I2C device; "
                                    "on a laptop use --mock") from exc
        except OSError as exc:
            raise DriverUnavailable(
                f"no PCA9685 at I2C address 0x{cfg.i2c_address:02x} on bus {cfg.i2c_bus} ({exc}). Check the "
                f"wiring, that I2C is enabled (sudo raspi-config nonint do_i2c 0), and "
                f"`i2cdetect -y {cfg.i2c_bus}`."
            ) from exc
        atexit.register(self.rest_now)
        self.rest_now()

    def pulse_for(self, angle: float) -> float:
        cfg = self.cfg
        return cfg.min_pulse_us + (cfg.max_pulse_us - cfg.min_pulse_us) * angle / 180.0

    def _write(self, channel: int, angle: float) -> None:
        pulse = self.pulse_for(angle)
        self.chip.set_pulse_us(channel, pulse)
        log.debug("servo ch%d -> %.0f deg (%.0f us)", channel, angle, pulse)


def make_driver(cfg: ActuatorConfig, kind: str | None = None) -> TapDriver:
    """"servo" uses the Adafruit library when it is installed and the built-in
    driver when it is not; "pca9685" always uses the built-in one."""
    kind = kind or cfg.driver
    if kind == "mock":
        return MockDriver(cfg)
    if kind == "servo" and importlib.util.find_spec("adafruit_servokit") is not None:
        return ServoDriver(cfg)
    if kind in ("servo", "pca9685"):
        return Pca9685Driver(cfg)
    raise ValueError(f"unknown driver: {kind!r}")
