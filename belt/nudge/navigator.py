"""The navigation pipeline: sensor sample -> pose -> follower -> taps.

Navigator is synchronous and has no clock: every input carries its own
time. The live runtime, the simulator and log replay all drive this same
object, so they cannot disagree about what a walk does.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .config import Config
from .deadreckon import DeadReckoner, Localizer, Pose
from .eventlog import EventLog
from .follower import Action, Follower, FollowerError, State
from .geometry import circular_mean_deg, circular_spread_deg
from .obstacle import ObstacleGuard
from .route import RouteError, Waypoints, extract_floors, finish_route, join_route
from .sensors.base import SensorSample


MIN_HEIGHT_CM, MAX_HEIGHT_CM = 120.0, 215.0


class NavError(RuntimeError):
    """A command that cannot be carried out right now (reported to the caller)."""


class Navigator:
    def __init__(self, cfg: Config, log: EventLog | None = None) -> None:
        self.cfg = cfg
        self.log = log or EventLog()
        self.loc = Localizer(cfg.deadreckon)
        self.follower = Follower(cfg.follower)
        self.guard = ObstacleGuard(cfg.obstacle)
        self.waypoints: Waypoints = []  # the stretch being walked: this floor's part of the route
        self.floors: list[tuple[Any, Waypoints]] = []  # (floor, waypoints) for each stretch, in walking order
        self.floor_index = 0
        self.route_name: str | None = None
        self.route_given: list[list[Any]] = []  # the loaded route as it came: [[x, y, floor], ...], every floor
        self.route_info: dict[str, int] = {}
        self.last: SensorSample | None = None
        self.sensor_ok = False
        self.paused = False  # stopped once because the sensors went quiet
        self.samples = 0
        self.taps: deque[dict[str, Any]] = deque(maxlen=40)
        self.events: deque[dict[str, Any]] = deque(maxlen=40)  # localizer snaps and calibrations
        self._seq = 0
        self._recent: deque[tuple[float, float]] = deque()
        self._last_tap_t: float | None = None
        self._base_step_m = cfg.deadreckon.step_length_m  # as measured on the reference wearer
        self.height_cm = cfg.deadreckon.height_ref_cm
        self.log.write("meta", version=1, config=cfg.to_dict())

    @property
    def dr(self) -> DeadReckoner:
        return self.loc.dr

    @property
    def pose(self) -> Pose | None:
        return self.loc.pose

    # -- commands ----------------------------------------------------------

    def _refuse_while_guiding(self, what: str) -> None:
        """A new route or calibration puts the follower back to IDLE. Doing
        that mid-walk would end guidance with no signal to the wearer, so it
        has to be asked for explicitly with a stop (which taps both pods)."""
        if self.follower.state is State.NAVIGATING:
            raise NavError(f"guidance is running: stop first, then {what}")

    def _read_route(self, data: Any) -> tuple[list[tuple[Any, Waypoints]], list[list[Any]], list[list[float]], int]:
        """A route as it will be walked: (floor, waypoints) for each stretch.
        Also the points as they were given, with and without their floors
        (for the log), and how many points will not be walked."""
        runs = extract_floors(data)
        if not runs:
            raise RouteError("a route needs at least two distinct waypoints")
        r = self.cfg.route
        floors = [(runs[0][0], finish_route(runs[0][1], flip_y=r.flip_y, simplify_m=r.simplify_m))]
        dropped = 0
        for floor, points in runs[1:]:
            if not r.floors:
                dropped += len(points)  # route.floors = false: the walk ends at the first change of floor
                continue
            try:
                floors.append((floor, finish_route(points, flip_y=r.flip_y, simplify_m=r.simplify_m)))
            except RouteError:
                dropped += len(points)  # a single point on a floor: nothing to walk there
        given = [[x, y, floor] for floor, points in runs for x, y in points]
        return floors, given, runs[0][1], dropped

    def _use_floor(self, index: int) -> None:
        """Make one floor's stretch the route being walked, from its start. Clears the calibration."""
        self.floor_index = index
        self.waypoints = self.floors[index][1]
        self.follower.set_route(self.waypoints, more_floors=index + 1 < len(self.floors))
        self.loc.set_route(self.waypoints)

    def set_route(self, data: Any, name: str | None = None, t: float | None = None) -> None:
        self._refuse_while_guiding("load the route")
        try:
            floors, given, first_given, dropped = self._read_route(data)
        except RouteError as exc:
            raise NavError(str(exc)) from None
        self.floors = floors
        self.route_given = given
        own = data.get("name") if isinstance(data, dict) else None
        self.route_name = name or (own if isinstance(own, str) else None)
        self.route_info = {"given": len(given), "used": len(floors[0][1]), "dropped_other_floors": dropped,
                           "floors": len(floors)}
        self._use_floor(0)  # also clears the calibration: a new route needs a new one
        # The log keeps the points as they were given (not yet flipped or
        # simplified), so a replay processes them the same way. `points` has
        # every floor; it is only written when there is more than one.
        more = {"points": given} if len(floors) > 1 else {}
        self.log.write("route", t=t, name=self.route_name, waypoints=first_given, **more)

    def reroute(self, data: Any, name: str | None = None, t: float | None = None) -> dict[str, Any]:
        """A new route for a walk that is already under way: the position,
        the calibration and the step length are kept, and guidance carries on
        (or waits for Start, if it was not running). The route is picked up
        from where the wearer is."""
        pose = self.loc.pose
        if pose is None:
            raise NavError("not calibrated: there is no position to carry on from. Load it as a route and calibrate")
        try:
            floors, given, _, dropped = self._read_route(data)
            first = join_route((pose.x, pose.y), floors[0][1], self.cfg.route.rejoin_m)
        except RouteError as exc:
            raise NavError(str(exc)) from None
        floors[0] = (floors[0][0], first)
        guiding = self.follower.state in (State.NAVIGATING, State.FLOOR)
        self.floors, self.floor_index, self.waypoints = floors, 0, first
        self.route_given = given
        own = data.get("name") if isinstance(data, dict) else None
        self.route_name = name or (own if isinstance(own, str) else None)
        self.route_info = {"given": len(given), "used": len(first), "dropped_other_floors": dropped,
                           "floors": len(floors)}
        self.loc.continue_on(first)
        self.follower.continue_on(first, navigating=guiding, more_floors=len(floors) > 1)
        self.log.write("reroute", t=t, name=self.route_name, points=given)
        return {"guiding": guiding, **self.route_info}

    def next_floor(self, t: float | None = None) -> dict[str, Any]:
        """The wearer is on the next floor, standing where its stretch of the
        route begins. The position is put there and guidance carries on. The
        belt cannot tell this by itself: steps on stairs are not steps along
        a corridor, and a lift has none."""
        if not self.loc.calibrated:
            raise NavError("not calibrated")
        if self.floor_index + 1 >= len(self.floors):
            raise NavError("this route has no further floor")
        if self.follower.state not in (State.FLOOR, State.NAVIGATING, State.STOPPED):
            raise NavError("the walk is not under way: calibrate and start first")
        self.floor_index += 1
        floor, waypoints = self.floors[self.floor_index]
        self.waypoints = waypoints
        self.loc.continue_on(waypoints, at=waypoints[0])
        self.follower.continue_on(waypoints, navigating=True, more_floors=self.floor_index + 1 < len(self.floors))
        self.paused = False
        self.log.write("floor", t=t, index=self.floor_index)
        return {"floor": floor, "floor_index": self.floor_index, "floors": len(self.floors),
                "waypoints": len(waypoints)}

    def calibrate(self, t: float | None = None) -> dict[str, float]:
        """The wearer stands on waypoint 0 facing waypoint 1."""
        self._refuse_while_guiding("calibrate")
        if not self.waypoints:
            raise NavError("no route loaded")
        if self.last is None or not self.sensor_ok:
            raise NavError("no live sensor data to calibrate against")
        if self.floor_index:
            self._use_floor(0)  # calibration is at the very start of the route, on its first floor
        # With smoothing on, average over the same span, so the reference is
        # the very value the next reading will be compared with.
        window = max(self.cfg.sensors.calibrate_window_s, self.cfg.sensors.heading_smooth_s)
        recent = [h for (ts, h) in self._recent if self.last.t - ts <= window] or [self.last.heading_deg]
        compass = circular_mean_deg(recent)
        heading = self.loc.reset(compass, self.last.step_count, self.last.t).heading_deg
        self.follower.reset()
        self.paused = False
        self.log.write("calibrate", t=t, compass_deg=compass, heading_deg=heading)
        return {"compass_deg": compass, "heading_deg": heading}

    def set_wearer_height(self, height_cm: float, t: float | None = None) -> float:
        """Someone else is wearing it: scale the step length to their height.

        Step length is taken as proportional to height, anchored on the step
        measured for the reference wearer (step_length_m at height_ref_cm).
        It applies from the next step, and to every calibration after it.
        Returns the new step length."""
        if isinstance(height_cm, bool) or not isinstance(height_cm, (int, float)) or not MIN_HEIGHT_CM <= height_cm <= MAX_HEIGHT_CM:
            raise NavError(f"height must be a number of centimetres between {MIN_HEIGHT_CM:.0f} and {MAX_HEIGHT_CM:.0f}")
        step = self._base_step_m * float(height_cm) / self.cfg.deadreckon.height_ref_cm
        self.height_cm = float(height_cm)
        self.cfg.deadreckon.step_length_m = step  # the localizer shares this config: used at calibration
        self.loc.dr.step_length_m = step
        self.log.write("wearer", t=t, height_cm=self.height_cm, step_length_m=step)
        return step

    def mark(self, waypoint: int | None = None, t: float | None = None) -> dict[str, Any]:
        """Someone watching says the wearer is standing on a waypoint now
        (the next one, unless told which). The position is put there, whatever
        the step count made of the leg. Allowed while guiding: that is when
        it is needed."""
        if not self.loc.calibrated or self.last is None:
            raise NavError("not calibrated: a mark corrects a position, and there is none yet")
        last = len(self.waypoints) - 1
        if waypoint is None:
            waypoint = min(self.loc.leg + 1, last)
        if isinstance(waypoint, bool) or not isinstance(waypoint, int) or not 1 <= waypoint <= last:
            raise NavError(f"waypoint must be a whole number from 1 to {last} (0 is where calibration puts the wearer)")
        self.log.write("mark", t=t, waypoint=waypoint)
        before = self.loc.step_length_m
        pose = self.loc.mark(waypoint, self.last.t)
        moved = self._drain_events()[0]["moved_m"]
        self.follower.aim_at(self.loc.leg + 1, resume=waypoint < last)
        return {"waypoint": waypoint, "moved_m": moved, "x": pose.x, "y": pose.y,
                "step_length_m": self.loc.step_length_m, "step_length_was_m": before}

    def _drain_events(self) -> list[dict[str, Any]]:
        """Move the localizer's events to the log and to the list the viewer shows."""
        drained = []
        for event in self.loc.drain():
            name = event.pop("event")
            self._seq += 1
            self.events.append({"seq": self._seq, "event": name, **event})
            self.log.write(name, **event)
            drained.append(event)
        return drained

    def on_obstacle(self, left_m: Any, ahead_m: Any, right_m: Any, t: float) -> dict[str, Any]:
        """What a sensor looking ahead of the wearer sees: the nearest thing
        to the left, dead ahead and to the right, in metres (None: nothing in
        range). It takes effect with the next sensor sample."""
        try:
            self.guard.reading(t, left_m, ahead_m, right_m)
        except ValueError as exc:
            raise NavError(str(exc)) from None
        self.log.write("obstacle", t=t, left_m=self.guard.left_m, ahead_m=self.guard.ahead_m,
                       right_m=self.guard.right_m)
        return self.guard.info(t)

    def set_obstacle_mode(self, enabled: Any, t: float | None = None) -> bool:
        """The obstacle switch. Off, readings are still taken and shown but
        never change a tap; switching off mid-walk lets guidance go straight on."""
        if not isinstance(enabled, bool):
            raise NavError('send {"enabled": true} or {"enabled": false}')
        self.guard.enabled = enabled
        if not enabled:
            self.guard.blocked = False
        self.log.write("obstacle_mode", t=t, enabled=enabled)
        return enabled

    def start(self, t: float | None = None) -> None:
        if not self.loc.calibrated:
            raise NavError("not calibrated: stand on waypoint 0 facing waypoint 1 and calibrate first")
        try:
            self.follower.start()
        except FollowerError as exc:
            raise NavError(str(exc)) from None
        self.paused = False
        self.log.write("start", t=t)

    def stop(self, t: float | None = None) -> list[Action]:
        actions = self.follower.stop()
        self.log.write("stop", t=t)
        self._record(t, actions)
        return actions

    # -- sensor input ------------------------------------------------------

    def on_sample(self, s: SensorSample) -> list[Action]:
        self.last = s
        self.samples += 1
        self.sensor_ok = True
        self.paused = False
        self._recent.append((s.t, s.heading_deg))
        while self._recent and s.t - self._recent[0][0] > 2.0:
            self._recent.popleft()
        record: dict[str, Any] = {"t": s.t, "heading_deg": s.heading_deg, "step_count": s.step_count}
        if s.raw:
            record["raw"] = s.raw
        self.log.write("sensor", **record)
        if not self.loc.calibrated:
            return []

        blank = self.cfg.deadreckon.step_blank_after_tap_s
        count_steps = not (blank > 0 and self._last_tap_t is not None and s.t - self._last_tap_t < blank)
        pose = self.loc.update(s.t, self._heading(s), s.step_count, count_steps=count_steps)
        self._drain_events()
        self.follower.skip_to(self.loc.leg + 1)  # never steer back to a corner already turned
        actions = self.follower.update(s.t, pose.x, pose.y, pose.heading_deg)
        # Something in the way goes before the route: stop, then the way round.
        actions = self.guard.update(s.t, self.follower.state is State.NAVIGATING, actions)
        self.log.write(
            "pose", t=s.t, x=pose.x, y=pose.y, heading_deg=pose.heading_deg,
            state=self.follower.state.value, target=self.follower.target,
            error_deg=self.follower.error_deg, leg=self.loc.leg, bias_deg=self.loc.bias_deg,
            step_length_m=self.loc.step_length_m, frozen=self.loc.frozen,
        )
        self._record(s.t, actions)
        return actions

    def _heading(self, s: SensorSample) -> float:
        """The heading used for position: the reading itself, or its average
        over the last heading_smooth_s. A phone in a pocket swings with every
        stride; averaging over about one stride leaves the direction of travel.
        The log keeps the raw reading, so replay smooths it the same way."""
        smooth = self.cfg.sensors.heading_smooth_s
        if smooth <= 0:
            return s.heading_deg
        return circular_mean_deg([h for (ts, h) in self._recent if s.t - ts <= smooth])

    def on_stale(self, t: float) -> list[Action]:
        """No sensor data for stale_after_s: stop the wearer, once."""
        self.sensor_ok = False
        if self.follower.state is not State.NAVIGATING or self.paused:
            return []
        self.paused = True
        self.log.write("stale", t=t)
        actions = [Action("stop")]
        self._record(t, actions)
        return actions

    def _record(self, t: float | None, actions: list[Action]) -> None:
        for action in actions:
            self._seq += 1
            self._last_tap_t = t
            why = {"reason": action.reason} if action.reason else {}
            self.taps.append({"seq": self._seq, "t": t, "kind": action.kind, "side": action.side, **why})
            self.log.write("tap", t=t, kind=action.kind, side=action.side, **why)

    # -- state for the viewer ---------------------------------------------

    def heading_spread_deg(self, window_s: float = 1.0) -> float:
        if not self._recent:
            return 0.0
        newest = self._recent[-1][0]
        return circular_spread_deg(h for (ts, h) in self._recent if newest - ts <= window_s)

    def sample_rate_hz(self, window_s: float = 1.0) -> float:
        if len(self._recent) < 2:
            return 0.0
        newest = self._recent[-1][0]
        times = [ts for (ts, _) in self._recent if newest - ts <= window_s]
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        pose = self.loc.pose
        f = self.follower
        sensor = None
        if self.last is not None:
            sensor = {
                "t": self.last.t,
                "heading_deg": self.last.heading_deg,
                "step_count": self.last.step_count,
                "age_s": None if now is None else max(0.0, now - self.last.t),
                "rate_hz": self.sample_rate_hz(),
                "spread_deg": self.heading_spread_deg(),
            }
        return {
            "now": now,
            "state": f.state.value,
            "calibrated": self.loc.calibrated,
            "sensor_ok": self.sensor_ok,
            "paused": self.paused,
            "pose": None if pose is None else {"x": pose.x, "y": pose.y, "heading_deg": pose.heading_deg},
            "target": f.target,
            "error_deg": f.error_deg,
            "distance_m": f.distance_m,
            "correcting": f.correcting,
            "route": {"name": self.route_name, "waypoints": [list(p) for p in self.waypoints],
                      "floor": self.floors[self.floor_index][0] if self.floors else None,
                      "floor_index": self.floor_index, "floors": len(self.floors)},
            "sensor": sensor,
            "taps": list(self.taps)[-20:],
            "events": list(self.events)[-20:],
            "localizer": self.loc.info(),
            "obstacle": self.guard.info(now),
            "wearer": {"height_cm": self.height_cm, "height_ref_cm": self.cfg.deadreckon.height_ref_cm,
                       "min_cm": MIN_HEIGHT_CM, "max_cm": MAX_HEIGHT_CM},
            "limits": {
                "arrive_radius_m": self.cfg.follower.arrive_radius_m,
                "enter_deg": self.cfg.follower.enter_deg,
                "exit_deg": self.cfg.follower.exit_deg,
                "urgent_deg": self.cfg.follower.urgent_deg,
            },
        }
