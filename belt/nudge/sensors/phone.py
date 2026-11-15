"""Sensor readings from the phone page (web/phone.html).

PhoneSource does not know how messages arrive. The server hands it decoded
JSON through feed(), from the WebSocket or from POST /sensor, so the
transport (USB tether, hotspot, HTTPS tunnel, ...) can change without
touching anything downstream.

One phone at a time: the first client to send a usable reading is the
sensor, and readings from any other client are refused for as long as that
one keeps sending. Two phones interleaved would give a heading and a step
count that jump between two devices.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, AsyncIterator

from .base import SensorSample, SensorSource

log = logging.getLogger("nudge")

_HEADING_KEY = {"compass": "compass_deg", "gyro": "yaw_deg"}

OK = "ok"
INVALID = "invalid"  # not a usable reading
OLD = "old"  # the phone says its own sensors stopped delivering
BUSY = "busy"  # another phone is the sensor right now


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class PhoneSource(SensorSource):
    name = "phone"

    def __init__(self, heading_source: str = "compass", takeover_s: float = 1.0, max_age_s: float = 1.0) -> None:
        self.heading_source = heading_source
        self.takeover_s = takeover_s  # a silent client loses its place after this long
        self.max_age_s = max_age_s  # readings the phone itself calls older than this are refused
        self._queue: asyncio.Queue[SensorSample] = asyncio.Queue(maxsize=200)
        self.received = 0
        self.rejected = 0
        self.transport: str | None = None
        self.client: str | None = None  # who is the sensor right now
        self.last_status = OK
        self._client_t = 0.0
        self._steps_in: int | None = None  # the active client's own count
        self._steps_out: int | None = None  # the continuous count handed downstream

    def _refuse(self, status: str) -> bool:
        self.rejected += 1
        self.last_status = status
        return False

    def feed(self, msg: Any, transport: str = "ws", client: str | None = None) -> bool:
        """Accept one message from the phone. Returns False if it was not
        used; last_status then says why (INVALID, OLD or BUSY)."""
        if not isinstance(msg, dict):
            return self._refuse(INVALID)
        heading = _number(msg.get(_HEADING_KEY[self.heading_source]))
        if heading is None:
            heading = _number(msg.get("heading_deg"))
        steps = _number(msg.get("step_count"))
        if heading is None or steps is None or steps < 0:
            return self._refuse(INVALID)
        age_ms = _number(msg.get("age_ms"))
        if age_ms is not None and age_ms > self.max_age_s * 1000.0:
            return self._refuse(OLD)

        # A page load names itself with "sid", so it stays the same client
        # when it moves between the WebSocket and POST.
        sid = msg.get("sid")
        who = sid[:64] if isinstance(sid, str) and sid else (client or transport)
        now = self.now()
        if who != self.client:
            if self.client is not None and now - self._client_t <= self.takeover_s:
                return self._refuse(BUSY)
            if self.client is not None:
                log.warning("sensor phone changed: stop and calibrate again if this is a different phone")
            self.client = who
            self._steps_in = None  # a different page counts from its own zero

        # The first phone's count is used as it is. After that only the change
        # in the active client's count is passed on, so a page reload or a
        # change of phone never arrives as a jump in steps.
        count = int(steps)
        if self._steps_out is None:
            self._steps_out = count
        elif self._steps_in is not None and count > self._steps_in:
            self._steps_out += count - self._steps_in
        self._steps_in = count
        self._client_t = now

        sample = SensorSample(round(now, 4), heading % 360.0, self._steps_out, raw=msg)
        if self._queue.full():
            self._queue.get_nowait()  # never let a backlog delay fresh readings
        self._queue.put_nowait(sample)
        self.received += 1
        self.transport = transport
        self.last_status = OK
        return True

    async def samples(self) -> AsyncIterator[SensorSample]:
        while True:
            yield await self._queue.get()
