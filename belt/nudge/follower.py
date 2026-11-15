"""Route follower: a pure state machine.

It is given a pose and a time, and answers with the taps to play. It does no
I/O and keeps no clock of its own, so the same inputs always give the same
taps (which is what makes a logged walk replayable).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .config import FollowerConfig
from .geometry import bearing_deg, wrap180
from .route import Waypoints


class State(str, Enum):
    IDLE = "IDLE"
    NAVIGATING = "NAVIGATING"
    ARRIVED = "ARRIVED"
    STOPPED = "STOPPED"
    FLOOR = "FLOOR"  # at the end of this floor's stretch, waiting to be told the wearer is on the next floor


@dataclass(frozen=True)
class Action:
    kind: str  # "tap", "double_tap", "stop" or "arrived"
    side: str | None = None  # "left" or "right" for tap and double_tap
    # "overturn": the wearer turned past the target and is being called back.
    # "floor": a stop because the route changes floor here.
    reason: str | None = None


RATE_TAU_S = 0.15  # the turning rate is smoothed over about this long
RATE_GAP_S = 1.0  # readings further apart than this say nothing about the rate


class FollowerError(RuntimeError):
    pass


class Follower:
    def __init__(self, cfg: FollowerConfig | None = None) -> None:
        self.cfg = cfg or FollowerConfig()
        self.waypoints: Waypoints = []
        self.state = State.IDLE
        self.target = 1  # index of the waypoint being walked towards
        self.correcting = False
        self.error_deg: float | None = None
        self.distance_m: float | None = None
        self._last_tap_t: float | None = None
        self._last_side: str | None = None
        self._last_tap_target: int | None = None
        self._prev: tuple[float, float] | None = None  # (time, heading) of the last update
        self.turn_rate_deg_s = 0.0  # how fast the wearer is turning; positive is clockwise (to the right)
        self.more_floors = False  # the route goes on on another floor after these waypoints

    def set_route(self, waypoints: Waypoints, more_floors: bool = False) -> None:
        if len(waypoints) < 2:
            raise FollowerError("a route needs at least two waypoints")
        self.waypoints = list(waypoints)
        self.more_floors = more_floors
        self.reset()

    def continue_on(self, waypoints: Waypoints, navigating: bool, more_floors: bool = False) -> None:
        """Other waypoints, for a walk that is already under way: a new route
        from where the wearer is, or the next floor's stretch. Guidance goes
        on without a calibration (or waits for Start, if it was not running)."""
        if len(waypoints) < 2:
            raise FollowerError("a route needs at least two waypoints")
        self.waypoints = list(waypoints)
        self.more_floors = more_floors
        self.target = 1
        self.correcting = False
        self.error_deg = None
        self.distance_m = None
        self._last_tap_target = None  # a tap the other way now is a new direction, not an overturn
        self.state = State.NAVIGATING if navigating else State.IDLE

    def reset(self) -> None:
        """Back to the start of the route: IDLE, heading for waypoint 1."""
        self.state = State.IDLE
        self.target = 1
        self.correcting = False
        self.error_deg = None
        self.distance_m = None
        self._last_tap_t = None
        self._last_side = None
        self._last_tap_target = None
        self._prev = None
        self.turn_rate_deg_s = 0.0

    def start(self) -> None:
        if not self.waypoints:
            raise FollowerError("no route loaded")
        if self.state is State.ARRIVED:
            raise FollowerError("already arrived: calibrate at the start to walk the route again")
        if self.state is State.FLOOR:
            raise FollowerError("waiting at a change of floor: say when the wearer is on the next floor")
        self.state = State.NAVIGATING

    def stop(self) -> list[Action]:
        """Operator stop. Signals the wearer only if they were being guided."""
        if self.state is State.FLOOR:
            self.state = State.STOPPED  # already standing still at the change of floor: nothing to signal
            return []
        if self.state is not State.NAVIGATING:
            return []
        self.state = State.STOPPED
        self.correcting = False
        return [Action("stop")]

    def skip_to(self, target: int) -> None:
        """Never aim at a waypoint behind the leg the wearer is known to be on."""
        if self.waypoints and target > self.target:
            self.target = min(target, len(self.waypoints) - 1)

    def aim_at(self, target: int, resume: bool = False) -> None:
        """The wearer was just placed on a waypoint by hand: walk towards this
        one from there, even if it is one the follower had already ticked off.
        `resume`: the wearer is not at the end, so if arrival had already been
        declared (the step count ran ahead of them), guidance carries on."""
        if not self.waypoints:
            return
        self.target = min(max(target, 1), len(self.waypoints) - 1)
        self.correcting = False
        if resume and self.state is State.ARRIVED:
            self.state = State.NAVIGATING

    def _track_turning(self, t: float, heading_deg: float) -> None:
        prev, self._prev = self._prev, (t, heading_deg)
        if prev is None or not 0 < t - prev[0] <= RATE_GAP_S:
            self.turn_rate_deg_s = 0.0
            return
        dt = t - prev[0]
        now = wrap180(heading_deg - prev[1]) / dt
        self.turn_rate_deg_s += (1.0 - math.exp(-dt / RATE_TAU_S)) * (now - self.turn_rate_deg_s)

    def update(self, t: float, x: float, y: float, heading_deg: float) -> list[Action]:
        if self.state is not State.NAVIGATING:
            self._prev = None
            return []
        cfg = self.cfg
        self._track_turning(t, heading_deg)

        # Advance past every waypoint already within reach.
        while True:
            wx, wy = self.waypoints[self.target]
            dx, dy = wx - x, wy - y
            dist = math.hypot(dx, dy)
            if dist > cfg.arrive_radius_m:
                break
            if self.target == len(self.waypoints) - 1:
                self.correcting = False
                self.distance_m = dist
                self.error_deg = 0.0
                if self.more_floors:
                    # The stairs or the lift, not the destination: stop the
                    # wearer here and wait to be told they are on the next floor.
                    self.state = State.FLOOR
                    return [Action("stop", None, "floor")]
                self.state = State.ARRIVED
                return [Action("arrived")]
            self.target += 1

        err = wrap180(bearing_deg(dx, dy) - heading_deg)
        self.error_deg = err
        self.distance_m = dist
        mag = abs(err)

        # Deadband with hysteresis: start correcting above enter_deg, and
        # keep correcting until the error is back under exit_deg.
        if self.correcting:
            if mag < cfg.exit_deg:
                self.correcting = False
        elif mag > cfg.enter_deg:
            self.correcting = True
        if not self.correcting:
            return []

        side = "right" if err > 0 else "left"
        if mag > 170.0 and self._last_side:
            side = self._last_side  # facing backwards: do not flip sides on noise
        since = None if self._last_tap_t is None else t - self._last_tap_t

        # Overturn: tapped one way a moment ago for this same waypoint, and now
        # the error is on the other side. The wearer turned past it. They are
        # called back sooner than the usual period, and with a single tap
        # whatever the size: a double tap asks for a big turn, and a big turn
        # is what just went wrong.
        overturn = (cfg.overturn_window_s > 0 and since is not None and since <= cfg.overturn_window_s
                    and self._last_side is not None and side != self._last_side
                    and self._last_tap_target == self.target)
        if since is not None and since < (cfg.overturn_gap_s if overturn else cfg.tap_period_s):
            return []
        # Already turning the way a tap would ask: they have the message. A
        # second tap now would be answered on top of the first, and that is
        # how a turn becomes an overturn.
        towards = self.turn_rate_deg_s if side == "right" else -self.turn_rate_deg_s
        if cfg.turning_deg_s > 0 and towards >= cfg.turning_deg_s:
            return []
        self._last_tap_t = t
        self._last_side = side
        self._last_tap_target = self.target
        if overturn:
            return [Action("tap", side, "overturn")]
        return [Action("double_tap" if mag > cfg.urgent_deg else "tap", side)]
