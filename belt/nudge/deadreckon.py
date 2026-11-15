"""Position from compass heading and step count.

DeadReckoner is plain step integration: each step advances step_length_m
along the current heading.

Localizer wraps it with drift correction that uses the route:

  route snap    within snap_dist_m of the current leg, the pose is projected
                onto the leg;
  turn snap     a heading change within turn_tol_deg of the turn expected at
                the next waypoint, made in turn_time_s or less while within
                turn_window_m of it, puts the pose on that waypoint and moves
                to the next leg. The change must still be there turn_hold_s
                later and must leave the wearer facing along the next leg, so
                that one bad compass reading or a swing from one side of the
                leg to the other is not taken for the turn;
  step length   after the first turn snap, step_length_m becomes first leg
                length / steps counted on it, clamped;
  compass bias  while walking a leg, the difference between heading and leg
                bearing is low-passed and subtracted from the heading; the
                estimate is reset at each turn;
  freeze        with no step for freeze_after_s the pose is frozen.

A step count that jumps by more than anyone can walk between two readings is
ignored rather than applied (step_jump).

Every snap and calibration is reported as an event for the JSONL log.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from .config import DeadReckonConfig
from .geometry import bearing_deg, unit, wrap180, wrap360
from .route import Waypoints


@dataclass
class Pose:
    x: float
    y: float
    heading_deg: float


class DeadReckoner:
    def __init__(self, step_length_m: float = 0.7) -> None:
        self.step_length_m = step_length_m
        self.pose: Pose | None = None
        self._ref_compass = 0.0
        self._ref_heading = 0.0
        self._last_steps = 0

    @property
    def calibrated(self) -> bool:
        return self.pose is not None

    def reset(self, x: float, y: float, heading_deg: float, compass_deg: float, step_count: int) -> Pose:
        """Place the wearer at (x, y) facing heading_deg in the building
        frame. compass_deg is what the compass reads in that stance; from now
        on only its change matters, so north offset, declination and the
        phone's mounting angle all cancel."""
        self._ref_compass = compass_deg
        self._ref_heading = heading_deg
        self._last_steps = step_count
        self.pose = Pose(x, y, wrap360(heading_deg))
        return self.pose

    def clear(self) -> None:
        self.pose = None

    def frame_heading(self, compass_deg: float) -> float:
        return wrap360(self._ref_heading + compass_deg - self._ref_compass)

    def take_steps(self, step_count: int) -> int:
        """New steps since the last reading. A count that goes backwards
        means the phone page restarted: re-base without moving."""
        delta = step_count - self._last_steps
        self._last_steps = step_count
        return max(delta, 0)

    def advance(self, steps: int, heading_deg: float) -> None:
        assert self.pose is not None
        ux, uy = unit(heading_deg)
        self.pose.x += steps * self.step_length_m * ux
        self.pose.y += steps * self.step_length_m * uy

    def update(self, compass_deg: float, step_count: int, count_steps: bool = True) -> Pose:
        if self.pose is None:
            raise RuntimeError("not calibrated")
        heading = self.frame_heading(compass_deg)
        steps = self.take_steps(step_count)
        if steps and count_steps:
            self.advance(steps, heading)
        self.pose.heading_deg = heading
        return self.pose


MIN_STEPS_FOR_STEP_LENGTH = 3  # do not trust a leg walked in fewer steps
ROUTE_SNAP_LOG_M = 0.01  # smaller projections are applied but not logged
FALLBACK_STEPS = 2  # steps along the next leg that prove an uncaught turn
MAX_STEP_RATE_HZ = 3.0  # the phone page cannot report steps faster than this
STEP_BURST_SLACK = 2  # steps allowed in one reading on top of that rate
STEP_BURST_GAP_S = 2.0  # a longer silence earns no more allowance: 8 steps at most
MARK_STEP_TRUST = 0.3  # a step length measured at a mark is used only within 30 percent of the current one


class Localizer:
    """Dead reckoning plus route-aware drift correction. Pure: no clock, no I/O."""

    def __init__(self, cfg: DeadReckonConfig | None = None, waypoints: Waypoints | None = None) -> None:
        self.cfg = cfg or DeadReckonConfig()
        self.dr = DeadReckoner(self.cfg.step_length_m)
        self.waypoints: Waypoints = []
        self.leg = 0  # walking from waypoints[leg] to waypoints[leg + 1]
        self.bias_deg = 0.0
        self.frozen = False
        self.events: list[dict[str, Any]] = []
        self._history: deque[tuple[float, float]] = deque()  # (t, heading before bias correction)
        # A turn that has been seen but not yet confirmed: (time and heading
        # it is measured from, time it was first seen).
        self._turn: tuple[float, float, float] | None = None
        self._t_prev: float | None = None
        self._t_last_step = 0.0
        self._steps_on_leg = 0
        self._fallback_steps = 0
        self._first_turn_done = False
        # The last place the pose was put on a known point (calibration, a
        # turn snap, a leg advance, a mark): where, on which leg, and the
        # steps counted since. Only mark() reads it.
        self._fix: tuple[float, float, int] | None = None
        self._steps_since_fix = 0
        if waypoints:
            self.set_route(waypoints)

    # -- setup ---------------------------------------------------------------

    @property
    def pose(self) -> Pose | None:
        return self.dr.pose

    @property
    def calibrated(self) -> bool:
        return self.dr.calibrated

    @property
    def step_length_m(self) -> float:
        return self.dr.step_length_m

    def set_route(self, waypoints: Waypoints) -> None:
        if len(waypoints) < 2:
            raise ValueError("a route needs at least two waypoints")
        self.waypoints = list(waypoints)
        self.dr.clear()

    def reset(self, compass_deg: float, step_count: int, t: float) -> Pose:
        """Calibrate: the wearer stands on waypoint 0 facing waypoint 1."""
        if len(self.waypoints) < 2:
            raise RuntimeError("no route")
        x0, y0 = self.waypoints[0]
        heading = self._bearing(0)
        self.dr.step_length_m = self.cfg.step_length_m
        self.leg = 0
        self.bias_deg = 0.0
        self.frozen = False
        self.events.clear()
        self._history.clear()
        self._history.append((t, heading))
        self._turn = None
        self._t_prev = t
        self._t_last_step = t
        self._steps_on_leg = 0
        self._fallback_steps = 0
        self._first_turn_done = False
        self._fixed(x0, y0)
        return self.dr.reset(x0, y0, heading, compass_deg, step_count)

    def _fixed(self, x: float, y: float) -> None:
        self._fix = (x, y, self.leg)
        self._steps_since_fix = 0

    def continue_on(self, waypoints: Waypoints, at: tuple[float, float] | None = None) -> Pose:
        """Carry on along other waypoints without calibrating again: the
        compass reference, the step count and the step length stay as they
        are. `at` puts the position there first (the start of the next floor's
        stretch); without it the position is kept (a new route mid-walk)."""
        pose = self.dr.pose
        if pose is None:
            raise RuntimeError("not calibrated")
        if len(waypoints) < 2:
            raise ValueError("a route needs at least two waypoints")
        self.waypoints = list(waypoints)
        self.leg = 0
        self.bias_deg = 0.0
        self._turn = None
        self._steps_on_leg = 0
        self._fallback_steps = 0
        self._first_turn_done = True  # this first leg did not start at a calibration: it measures nothing
        self._history.clear()
        if at is not None:
            pose.x, pose.y = at
            self._fixed(pose.x, pose.y)
        else:
            self._fix = None  # an estimate, not a known point: a mark must not measure a step from it
            self._steps_since_fix = 0
        return pose

    def mark(self, waypoint: int, t: float) -> Pose:
        """Someone watching says the wearer is standing on this waypoint now.

        The one correction that depends on neither the step count nor the
        step length: the pose goes to the waypoint and guidance carries on
        along the leg that leaves it. The heading is left alone, so if the
        wearer has not turned yet the follower taps them round.

        When the mark is the end of the leg being walked, and that leg was
        walked from a known point, the leg also measures the step length
        (distance / steps). It is used from here on if it is within
        MARK_STEP_TRUST of the current one; further off than that is more
        likely a miscount or a mark pressed early than a real stride."""
        pose = self.dr.pose
        if pose is None:
            raise RuntimeError("not calibrated")
        if not 1 <= waypoint <= self._legs:
            raise ValueError(f"waypoint must be between 1 and {self._legs}")
        wx, wy = self.waypoints[waypoint]
        measured = None
        if (waypoint == self.leg + 1 and self._fix is not None and self._fix[2] == self.leg
                and self._steps_since_fix >= MIN_STEPS_FOR_STEP_LENGTH):
            walked_m = math.hypot(wx - self._fix[0], wy - self._fix[1])
            measured = walked_m / self._steps_since_fix
        self._event("mark_snap", t, waypoint=waypoint, leg=self.leg,
                    moved_m=math.hypot(wx - pose.x, wy - pose.y), steps=self._steps_since_fix,
                    **{"from": [pose.x, pose.y], "to": [wx, wy]})
        pose.x, pose.y = wx, wy
        if measured is not None:
            old = self.dr.step_length_m
            used = abs(measured - old) <= MARK_STEP_TRUST * old
            self._event("step_length", t, old_m=old, new_m=measured if used else old, measured_m=measured,
                        leg_m=walked_m, steps=self._steps_since_fix, reason="mark", used=used)
            if used:
                self.dr.step_length_m = measured
        if waypoint < self._legs:
            self.leg = waypoint - 1
            self._next_leg()  # on the leg that leaves the waypoint, with nothing counted on it yet
        else:
            self.leg = self._legs - 1  # the last waypoint: still the last leg, standing on its end
            self._turn = None
        self._first_turn_done = True
        self._history.clear()  # a turn made before the mark must not count towards the next corner
        self._fixed(wx, wy)
        return pose

    def drain(self) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    def info(self) -> dict[str, Any]:
        return {"leg": self.leg, "bias_deg": self.bias_deg, "frozen": self.frozen,
                "step_length_m": self.dr.step_length_m, "drift_correction": self.cfg.drift_correction}

    # -- geometry ------------------------------------------------------------

    @property
    def _legs(self) -> int:
        return len(self.waypoints) - 1

    def _bearing(self, leg: int) -> float:
        (ax, ay), (bx, by) = self.waypoints[leg], self.waypoints[leg + 1]
        return bearing_deg(bx - ax, by - ay)

    def _along(self, leg: int) -> float:
        """Where the pose projects onto a leg: 0 at its start, 1 at its end."""
        (ax, ay), (bx, by) = self.waypoints[leg], self.waypoints[leg + 1]
        dx, dy = bx - ax, by - ay
        pose = self.dr.pose
        return ((pose.x - ax) * dx + (pose.y - ay) * dy) / (dx * dx + dy * dy)

    def _turn_window(self, leg: int) -> float:
        """How far from the waypoint at the end of a leg a turn may still be
        taken for the turn there. The step count's error grows with the
        distance walked, so a long leg gets a wider window."""
        (ax, ay), (bx, by) = self.waypoints[leg], self.waypoints[leg + 1]
        return max(self.cfg.turn_window_m, self.cfg.turn_window_frac * math.hypot(bx - ax, by - ay))

    def _turn_tol(self, expected_deg: float) -> float:
        """How close to the expected turn counts as the turn: turn_tol_deg,
        but never more than a third of the turn itself. At a 45 degree corner
        a 25 degree tolerance would accept a 20 degree veer as the turn."""
        return min(self.cfg.turn_tol_deg, abs(expected_deg) / 3.0)

    def _event(self, name: str, t: float, **fields: Any) -> None:
        self.events.append({"event": name, "t": t, **fields})

    def _next_leg(self) -> None:
        self.leg += 1
        self.bias_deg = 0.0  # "reset the estimate at each turn"
        self._steps_on_leg = 0
        self._fallback_steps = 0
        self._turn = None

    # -- the update ----------------------------------------------------------

    def update(self, t: float, compass_deg: float, step_count: int, count_steps: bool = True) -> Pose:
        if self.dr.pose is None:
            raise RuntimeError("not calibrated")
        cfg = self.cfg
        dr = self.dr
        pose = dr.pose
        raw = dr.frame_heading(compass_deg)
        gap = 0.0 if self._t_prev is None else max(t - self._t_prev, 0.0)
        self._t_prev = t
        dt = min(gap, 1.0)
        steps = dr.take_steps(step_count)
        allowed = STEP_BURST_SLACK + int(MAX_STEP_RATE_HZ * min(gap, STEP_BURST_GAP_S))
        if steps > allowed:
            # Nobody walks that fast: the count itself jumped (a phone glitch,
            # or readings resuming after a long silence). Moving the pose that
            # far along whatever the heading is now would be a guess, so the
            # steps are dropped and the count re-based.
            self._event("step_jump", t, steps=steps, allowed=allowed, gap_s=gap)
            steps = 0
        if not count_steps:
            steps = 0

        if not cfg.drift_correction:
            if steps:
                dr.advance(steps, raw)
            pose.heading_deg = raw
            return pose

        # Freeze: no step for freeze_after_s means the wearer is standing.
        if steps:
            self._t_last_step = t
        walking = t - self._t_last_step < (cfg.freeze_after_s if cfg.freeze_after_s > 0 else 1.5)
        frozen = cfg.freeze_after_s > 0 and not walking
        if frozen != self.frozen:
            self.frozen = frozen
            self._event("freeze" if frozen else "unfreeze", t, x=pose.x, y=pose.y)

        # Heading, with the compass bias taken out.
        if not (frozen and cfg.freeze_heading):
            pose.heading_deg = wrap360(raw - self.bias_deg)
        if steps:
            dr.advance(steps, pose.heading_deg)
            self._steps_on_leg += steps
            self._steps_since_fix += steps

        # Turns. The timed turn snap may fire while frozen: turning on the
        # spot at a corner is exactly the case it exists for.
        self._history.append((t, raw))
        while self._history and t - self._history[0][0] > cfg.turn_time_s:
            self._history.popleft()
        if self._turn_snap(t, raw):
            pose.heading_deg = raw  # the bias estimate was just reset
        elif steps:
            self._fallback_advance(t, steps, raw)

        if frozen:
            return pose
        on_leg = self._route_snap(t)

        # Compass bias: only while walking along the leg. That excludes standing,
        # turning off the leg (bias_max_deg), and the end of a turn before the
        # first step on the new leg.
        if cfg.bias_tau_s > 0 and walking and on_leg and self._steps_on_leg > 0 and dt > 0:
            diff = wrap180(raw - self._bearing(self.leg))
            if abs(diff) <= cfg.bias_max_deg:
                self.bias_deg += (1.0 - math.exp(-dt / cfg.bias_tau_s)) * (diff - self.bias_deg)
        return pose

    def _turn_snap(self, t: float, raw: float) -> bool:
        cfg = self.cfg
        turn, self._turn = self._turn, None
        if cfg.turn_window_m <= 0 or self.leg + 1 >= self._legs or self._steps_on_leg == 0:
            return False
        ahead = self._bearing(self.leg + 1)
        expected = wrap180(ahead - self._bearing(self.leg))
        if abs(expected) < cfg.min_turn_deg:
            return False
        pose = self.dr.pose
        wx, wy = self.waypoints[self.leg + 1]
        dist = math.hypot(wx - pose.x, wy - pose.y)
        if dist > self._turn_window(self.leg):
            return False

        tol = self._turn_tol(expected)

        def is_the_turn(h0: float) -> bool:
            return abs(wrap180(wrap180(raw - h0) - expected)) <= tol

        # 1. The heading has changed by about the expected turn, measured from
        #    a heading seen in the last turn_time_s.
        if turn is not None and not is_the_turn(turn[1]):
            turn = None
        if turn is None:
            for t0, h0 in self._history:
                if is_the_turn(h0):
                    turn = (t0, h0, t, self._steps_on_leg)
                    break
        self._turn = turn
        if turn is None:
            return False
        t0, h0, seen, steps = turn
        # 2. It is still there turn_hold_s later: one bad compass reading is
        #    not a turn.
        if t - seen < cfg.turn_hold_s - 1e-9:
            return False
        # 3. It leaves the wearer facing along the next leg: swinging from one
        #    side of this leg to the other (an over-correction) is not a turn.
        if abs(wrap180(raw - self.bias_deg - ahead)) > tol:
            return False

        # `steps` were on this leg when the turn was first seen. Any taken
        # since, while it was being confirmed, were taken after the corner.
        since = self._steps_on_leg - steps
        calibration = None
        ax, ay = self.waypoints[0]
        leg_m = math.hypot(wx - ax, wy - ay)
        if (cfg.calibrate_step_length and not self._first_turn_done and self.leg == 0
                and steps >= MIN_STEPS_FOR_STEP_LENGTH and leg_m >= cfg.calibrate_min_leg_m):
            measured = leg_m / steps
            new = min(max(measured, cfg.step_length_min_m), cfg.step_length_max_m)
            calibration = dict(old_m=self.dr.step_length_m, new_m=new, measured_m=measured, leg_m=leg_m, steps=steps)
        length = calibration["new_m"] if calibration else self.dr.step_length_m
        ux, uy = unit(ahead)
        to = [wx + since * length * ux, wy + since * length * uy]
        self._event("turn_snap", t, leg=self.leg, waypoint=self.leg + 1,
                    moved_m=math.hypot(to[0] - pose.x, to[1] - pose.y),
                    turn_deg=wrap180(raw - h0), expected_deg=expected, over_s=seen - t0, held_s=t - seen,
                    steps_on_leg=steps, steps_since=since, bias_deg=self.bias_deg,
                    **{"from": [pose.x, pose.y], "to": to})
        pose.x, pose.y = to
        if calibration:
            self._event("step_length", t, **calibration)
            self.dr.step_length_m = calibration["new_m"]
        self._first_turn_done = True
        self._next_leg()  # steps on the new leg count from zero: nothing is learned from the rest of the turn
        self._fixed(to[0], to[1])
        self._history.clear()  # the rest of this turn must not count towards the next one
        self._history.append((t, raw))
        return True

    def _fallback_advance(self, t: float, steps: int, raw: float) -> None:
        """Move to the next leg when the turn snap did not, so the route snap
        can never pin the pose to a leg the wearer has already left."""
        cfg = self.cfg
        pose = self.dr.pose
        while self.leg + 1 < self._legs:
            here, ahead = self._bearing(self.leg), self._bearing(self.leg + 1)
            wx, wy = self.waypoints[self.leg + 1]
            expected = wrap180(ahead - here)
            if abs(expected) < cfg.min_turn_deg:
                # A waypoint on a straight or a gentle bend: nothing to detect,
                # just carry on once the end of the leg has been passed.
                if self._along(self.leg) < 1.0:
                    return
                self._event("leg_advance", t, leg=self.leg, waypoint=self.leg + 1, reason="passed")
                self._next_leg()
                continue
            # A real corner. Steps taken facing along the next leg, close to
            # the corner, mean the wearer has already turned.
            near = math.hypot(wx - pose.x, wy - pose.y) <= max(self._turn_window(self.leg), cfg.snap_dist_m, 1.0)
            facing_next = abs(wrap180(pose.heading_deg - ahead)) <= self._turn_tol(expected)
            if not (near and facing_next):
                self._fallback_steps = 0
                return
            self._fallback_steps += steps
            if self._fallback_steps < FALLBACK_STEPS:
                return
            ux, uy = unit(ahead)
            walked = self._fallback_steps * self.dr.step_length_m
            to = [wx + walked * ux, wy + walked * uy]
            self._event("leg_advance", t, leg=self.leg, waypoint=self.leg + 1, reason="heading",
                        **{"from": [pose.x, pose.y], "to": to})
            pose.x, pose.y = to
            self._next_leg()
            self._fixed(to[0], to[1])
            self._history.clear()
            self._history.append((t, raw))
            return

    def _route_snap(self, t: float) -> bool:
        """Project the pose onto the current leg if it is close enough.
        Returns whether the pose counts as being on the leg."""
        cfg = self.cfg
        if cfg.snap_dist_m <= 0:
            return True
        pose = self.dr.pose
        (ax, ay), (bx, by) = self.waypoints[self.leg], self.waypoints[self.leg + 1]
        u = min(max(self._along(self.leg), 0.0), 1.0)
        cx, cy = ax + u * (bx - ax), ay + u * (by - ay)
        moved = math.hypot(pose.x - cx, pose.y - cy)
        if moved > cfg.snap_dist_m:
            return False
        if moved > ROUTE_SNAP_LOG_M:
            self._event("route_snap", t, leg=self.leg, moved_m=moved,
                        **{"from": [pose.x, pose.y], "to": [cx, cy]})
        pose.x, pose.y = cx, cy
        return True
