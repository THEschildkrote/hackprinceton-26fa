"""Placeholder for an on-board I2C compass/IMU (for example a BNO055 on the
same bus as the PCA9685), so the phone would no longer be needed.

To implement: read heading and acceleration at 10-20 Hz, run the same
peak-detection step counter as web/phone.html, and yield SensorSample.
"""

from __future__ import annotations

from typing import AsyncIterator

from .base import SensorSample, SensorSource


class ImuSource(SensorSource):
    name = "imu"

    def __init__(self, i2c_address: int = 0x28) -> None:
        self.i2c_address = i2c_address

    async def samples(self) -> AsyncIterator[SensorSample]:
        raise NotImplementedError("ImuSource is a stub: use the phone (default) or --sim")
        yield  # pragma: no cover
