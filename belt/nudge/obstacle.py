"""Something in the way that is not on the map: a person, a chair, a bag.

The route comes from a scan made when the room was empty. What is in the
room now has to come from a sensor looking ahead of the wearer. This module
does not care which one: a phone's LiDAR, an ultrasonic sensor on the belt,
a camera. It is given three distances (the nearest thing to the left, dead
ahead and to the right, in metres; None means nothing within the sensor's
range) and decides what the taps should say:

  ahead closer than stop_m   stop (both pods), once. The follower's own
                             steering taps are held back: walking on towards
                             the next waypoint is exactly what must not happen.
  still blocked              one tap a period towards the side with more room,
                             if that side has at least side_room_m. Boxed in
                             on both sides: nothing more, the wearer waits.
  ahead beyond clear_m       guidance carries on, and steers back to the route.

A reading older than stale_s is not believed: a sensor that has gone quiet
must not leave the wearer stopped, and must not be taken for a clear path
either, so the viewer shows that it is silent.

Pure: no clock, no I/O. Same readings and times in, same taps out.
"""

from __future__ import annotations

from typing import Any

from .config import ObstacleConfig
from .follower import Action


def _distance(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1000:
        raise ValueError(f"{name} must be a distance in metres, or null for nothing in range")
    return float(value)


class ObstacleGuard:
    def __init__(self, cfg: ObstacleConfig | None = None) -> None:
        self.cfg = cfg or ObstacleConfig()
        # The switch. Off: readings are still taken and shown, but they never
        # change a tap. It starts as the config says and can be flipped mid-walk.
        self.enabled = self.cfg.enabled
        self.t: float | None = None  # when the last reading came
        self.left_m: float | None = None
        self.ahead_m: float | None = None
        self.right_m: float | None = None
        self.blocked = False
        self._last_tap_t: float | None = None

    def reading(self, t: float, left_m: Any, ahead_m: Any, right_m: Any) -> None:
        left, ahead, right = _distance(left_m, "left_m"), _distance(ahead_m, "ahead_m"), _distance(right_m, "right_m")
        self.t, self.left_m, self.ahead_m, self.right_m = t, left, ahead, right

    def fresh(self, t: float) -> bool:
        return self.t is not None and 0 <= t - self.t <= self.cfg.stale_s

    def update(self, t: float, guiding: bool, actions: list[Action]) -> list[Action]:
        """The follower's taps for this moment go in; what should be played comes out."""
        cfg = self.cfg
        if not self.enabled or not guiding or not self.fresh(t):
            self.blocked = False
            return actions
        ahead = self.ahead_m
        limit = cfg.clear_m if self.blocked else cfg.stop_m  # once stopped, wait for a clearer path before going on
        if ahead is None or ahead >= limit:
            self.blocked = False
            return actions
        # An arrival or a stop from the follower still happens: only its steering is held back.
        passed = [a for a in actions if a.kind in ("arrived", "stop")]
        if passed:
            self.blocked = False
            return passed
        if not self.blocked:
            self.blocked = True
            self._last_tap_t = t
            return [Action("stop", None, "obstacle")]
        if self._last_tap_t is not None and t - self._last_tap_t < cfg.tap_period_s:
            return []
        side = self._open_side()
        if side is None:
            return []
        self._last_tap_t = t
        return [Action("tap", side, "obstacle")]

    def _open_side(self) -> str | None:
        """The side with more room, if it has enough. None in range counts as wide open."""
        room = self.cfg.side_room_m
        left = float("inf") if self.left_m is None else self.left_m
        right = float("inf") if self.right_m is None else self.right_m
        if max(left, right) < room:
            return None
        return "left" if left > right else "right"

    def info(self, now: float | None = None) -> dict[str, Any]:
        return {"left_m": self.left_m, "ahead_m": self.ahead_m, "right_m": self.right_m,
                "age_s": None if (now is None or self.t is None) else max(0.0, now - self.t),
                "blocked": self.blocked, "stop_m": self.cfg.stop_m, "clear_m": self.cfg.clear_m,
                "enabled": self.enabled}
