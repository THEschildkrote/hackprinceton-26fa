"""The sensor boundary: a source yields {t, heading_deg, step_count}."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator

_T0 = time.monotonic()


@dataclass(frozen=True)
class SensorSample:
    t: float  # seconds on the source's clock (monotonic)
    heading_deg: float  # compass-style, clockwise positive, any constant offset
    step_count: int  # cumulative
    raw: dict | None = None  # the original message, kept for the log


class SensorSource:
    """Async iterator of SensorSample.

    `now()` is the clock the samples are stamped with; `speed` is how many
    sample-seconds pass per wall-clock second (1 for real sensors).
    """

    name = "source"
    speed = 1.0

    def now(self) -> float:
        return time.monotonic() - _T0

    def __aiter__(self) -> AsyncIterator[SensorSample]:
        return self.samples()

    async def samples(self) -> AsyncIterator[SensorSample]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def close(self) -> None:
        pass
