"""Angles and the building frame.

The building frame is a flat plan view: x to the right, y up, metres.
Headings and bearings are compass-style: 0 deg points along +y and angles
grow clockwise, so +x is 90 deg. A positive heading error means "turn right".
"""

from __future__ import annotations

import math
from typing import Iterable


def wrap180(deg: float) -> float:
    """Wrap an angle to (-180, 180]."""
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def wrap360(deg: float) -> float:
    return deg % 360.0


def bearing_deg(dx: float, dy: float) -> float:
    """Compass-style bearing of the vector (dx, dy)."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


def unit(bearing: float) -> tuple[float, float]:
    r = math.radians(bearing)
    return math.sin(r), math.cos(r)


def circular_mean_deg(angles: Iterable[float]) -> float:
    s = c = 0.0
    for a in angles:
        s += math.sin(math.radians(a))
        c += math.cos(math.radians(a))
    return math.degrees(math.atan2(s, c)) % 360.0


def circular_spread_deg(angles: Iterable[float]) -> float:
    """Circular standard deviation, in degrees."""
    values = list(angles)
    if len(values) < 2:
        return 0.0
    s = sum(math.sin(math.radians(a)) for a in values) / len(values)
    c = sum(math.cos(math.radians(a)) for a in values) / len(values)
    r = min(1.0, math.hypot(s, c))
    if r <= 1e-12:
        return 180.0
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(r))))
