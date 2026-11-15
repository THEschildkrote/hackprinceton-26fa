"""The live loop: sensor source -> Navigator -> tap driver.

Also the one place commands (route, calibrate, start, stop) enter from the
server, the keyboard or a sim script, so they are all handled identically.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .actuators import TapDriver
from .config import Config
from .follower import Action
from .navigator import Navigator, NavError
from .route import RouteError, read_route_file, route_path, save_route_file
from .sensors.base import SensorSample, SensorSource

log = logging.getLogger("nudge")

_END = object()


class Runtime:
    def __init__(self, cfg: Config, nav: Navigator, source: SensorSource, driver: TapDriver,
                 routes_dir: Path, autostart: bool = False, autostart_delay_s: float = 0.0) -> None:
        self.cfg = cfg
        self.nav = nav
        self.source = source
        self.driver = driver
        self.routes_dir = routes_dir
        self.autostart = autostart
        self.autostart_delay_s = autostart_delay_s
        self._first_sample_t: float | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._tasks: set[asyncio.Task] = set()
        self._last_status = 0.0
        self._last_state = nav.follower.state

    # -- taps --------------------------------------------------------------

    def dispatch(self, actions: list[Action]) -> None:
        for action in actions:
            task = asyncio.create_task(self.driver.do(action.kind, action.side))
            self._tasks.add(task)
            task.add_done_callback(self._tap_done)

    def _tap_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("tap failed: %r", task.exception())

    # -- commands ----------------------------------------------------------

    def load_route(self, data: Any, name: str | None = None) -> None:
        self.nav.set_route(data, name=name, t=self.source.now())
        log.info("route %s: %d waypoints", self.nav.route_name or "(posted)", len(self.nav.waypoints))

    def reroute(self, data: Any) -> dict[str, Any]:
        info = self.nav.reroute(data, t=self.source.now())
        log.info("new route mid-walk: %d waypoints on this floor, %d floor(s), %s",
                 info["used"], info["floors"], "guiding" if info["guiding"] else "waiting for Start")
        return info

    def obstacle_mode(self, enabled: Any) -> bool:
        on = self.nav.set_obstacle_mode(enabled, self.source.now())
        log.info("obstacle stop switched %s", "on" if on else "off")
        return on

    def obstacle(self, left_m: Any, ahead_m: Any, right_m: Any) -> dict[str, Any]:
        return self.nav.on_obstacle(left_m, ahead_m, right_m, self.source.now())

    def next_floor(self) -> dict[str, Any]:
        info = self.nav.next_floor(self.source.now())
        log.info("next floor (%s): %d waypoints, guiding", info["floor"], info["waypoints"])
        return info

    def keep_route(self, name: Any) -> str:
        """Write the loaded route into the routes folder under a name. From
        then on it is in the viewer's list and loads with nothing but the Pi:
        no server, no laptop, and it is still there after a restart."""
        try:
            path = save_route_file(self.routes_dir, name, self.nav.route_given)
        except RouteError as exc:
            raise NavError(str(exc)) from None
        self.nav.route_name = name
        log.info("route kept as %s (%d points)", path.name, len(self.nav.route_given))
        return path.stem

    def load_route_named(self, name: str) -> None:
        try:
            path = route_path(self.routes_dir, name)
            self.load_route(read_route_file(path), name=path.stem)
        except RouteError as exc:
            raise NavError(str(exc)) from None

    def calibrate(self) -> dict[str, float]:
        info = self.nav.calibrate(self.source.now())
        log.info("calibrated: compass %.0f deg is route heading %.0f deg", info["compass_deg"], info["heading_deg"])
        return info

    def set_wearer_height(self, height_cm: float) -> float:
        step = self.nav.set_wearer_height(height_cm, self.source.now())
        log.info("wearer height %.0f cm: step length %.2f m", height_cm, step)
        return step

    def mark(self, waypoint: int | None = None) -> dict[str, Any]:
        info = self.nav.mark(waypoint, self.source.now())
        log.info("position fixed at waypoint %d (moved %.2f m, step length %.2f m)",
                 info["waypoint"], info["moved_m"], info["step_length_m"])
        return info

    def start(self) -> None:
        self.nav.start(self.source.now())
        log.info("navigating")

    def stop(self) -> None:
        self.dispatch(self.nav.stop(self.source.now()))
        log.info("stopped")

    def command(self, name: str) -> None:
        """A command from the keyboard or a script: errors are logged, not raised."""
        try:
            {"calibrate": self.calibrate, "start": self.start, "stop": self.stop,
             "next_floor": self.next_floor}[name]()
        except NavError as exc:
            log.warning("%s: %s", name, exc)

    def submit(self, item: Any) -> None:
        """Queue a sample or ("cmd", name) to be handled in order."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # -- the loop ----------------------------------------------------------

    async def _pump(self) -> None:
        try:
            async for sample in self.source:
                await self._queue.put(sample)
        finally:
            await self._queue.put(_END)

    async def run(self) -> None:
        if hasattr(self.source, "on_command"):
            self.source.on_command = lambda name: self.submit(("cmd", name))
        pump = asyncio.create_task(self._pump())
        timeout = self.cfg.sensors.stale_after_s / self.source.speed
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout)
                except asyncio.TimeoutError:
                    if self.nav.sensor_ok:
                        log.warning("sensor data stopped")
                    self._first_sample_t = None
                    self.dispatch(self.nav.on_stale(self.source.now()))
                    continue
                if item is _END:
                    break
                if isinstance(item, SensorSample):
                    self._on_sample(item)
                else:
                    self.command(item[1])
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            if self._tasks:
                await asyncio.wait(self._tasks, timeout=5)

    def _on_sample(self, sample: SensorSample) -> None:
        nav = self.nav
        if not nav.sensor_ok:
            log.info("sensor data flowing")
        self.dispatch(nav.on_sample(sample))
        if self._first_sample_t is None:
            self._first_sample_t = sample.t
        if self.autostart and nav.waypoints and sample.t - self._first_sample_t >= self.autostart_delay_s:
            self.autostart = False
            self.command("calibrate")
            self.command("start")
        state = nav.follower.state
        if state is not self._last_state:
            self._last_state = state
            log.info("state -> %s", state.value)
        if sample.t - self._last_status >= 1.0:
            self._last_status = sample.t
            log.info("%s", self.status_line())

    def status_line(self) -> str:
        nav = self.nav
        s = nav.last
        if s is None:
            return "no sensor data"
        line = (f"heading {s.heading_deg:5.1f} deg  steps {s.step_count:3d}  "
                f"{nav.sample_rate_hz():4.1f} Hz  jitter {nav.heading_spread_deg():4.1f} deg")
        pose = nav.dr.pose
        if pose is not None:
            f = nav.follower
            err = "   --" if f.error_deg is None else f"{f.error_deg:+5.0f}"
            line += (f"  |  pose ({pose.x:5.2f}, {pose.y:5.2f}) {round(pose.heading_deg) % 360:3d} deg  "
                     f"err {err}  wp {f.target}/{len(nav.waypoints) - 1}  {f.state.value}")
        return line
