"""Replay a JSONL walk log through a fresh Navigator.

The log's inputs (route, calibrate, start, stop, sensor, stale) are fed back
in order; the taps, poses and localizer events that come out are compared
with the ones recorded at the time. With the logged config they must match
exactly. With a different config the differences are the answer to "what
would this walk have done with these settings?".
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .eventlog import INPUT_TYPES, EventLog, read_log
from .navigator import Navigator, NavError
from .sensors.base import SensorSample

POSE_TOLERANCE = 1e-6


@dataclass
class ReplayResult:
    samples: int = 0
    logged_taps: list[tuple] = field(default_factory=list)  # (t, kind, side)
    replayed_taps: list[tuple] = field(default_factory=list)
    poses_compared: int = 0
    pose_mismatches: int = 0
    max_pose_error_m: float = 0.0
    logged_events: Counter = field(default_factory=Counter)
    replayed_events: Counter = field(default_factory=Counter)
    final_state: str = "IDLE"
    final_pose: tuple[float, float, float] | None = None
    errors: list[str] = field(default_factory=list)
    used_logged_config: bool = True

    @property
    def taps_match(self) -> bool:
        return self.logged_taps == self.replayed_taps

    @property
    def ok(self) -> bool:
        return (self.taps_match and self.pose_mismatches == 0 and not self.errors
                and self.logged_events == self.replayed_events)

    def summary(self) -> str:
        lines = [f"samples   {self.samples}"]
        lines.append(f"taps      {len(self.logged_taps)} logged, {len(self.replayed_taps)} replayed"
                     + (": identical" if self.taps_match else ": DIFFERENT"))
        lines.append(f"poses     {self.poses_compared} compared, {self.pose_mismatches} differ"
                     f" (largest difference {self.max_pose_error_m:.3f} m)")
        events = ", ".join(f"{name} {n}" for name, n in sorted(self.replayed_events.items())) or "none"
        lines.append(f"events    {events}" + ("" if self.logged_events == self.replayed_events else "  (log differs)"))
        if self.final_pose:
            x, y, h = self.final_pose
            lines.append(f"final     {self.final_state} at ({x:.2f}, {y:.2f}) facing {h:.0f} deg")
        else:
            lines.append(f"final     {self.final_state}, never calibrated")
        lines += [f"error     {e}" for e in self.errors]
        return "\n".join(lines)


def _tap(record: dict[str, Any]) -> tuple:
    return (record.get("t"), record.get("kind"), record.get("side"))


def replay(records: list[dict[str, Any]], cfg: Config | None = None) -> ReplayResult:
    result = ReplayResult()
    if cfg is None:
        meta = next((r for r in records if r.get("type") == "meta"), None)
        try:
            cfg = Config.from_dict(meta["config"]) if meta else Config.from_dict({})
        except (ConfigError, KeyError, TypeError) as exc:
            result.errors.append(f"logged config not usable ({exc}); used defaults")
            cfg = Config.from_dict({})
    else:
        result.used_logged_config = False

    out = EventLog(keep=True)
    nav = Navigator(cfg, out)
    for record in records:
        kind = record.get("type")
        t = record.get("t")
        try:
            if kind == "route":
                data = {"points": record["points"]} if record.get("points") else {"waypoints": record["waypoints"]}
                nav.set_route(data, name=record.get("name"), t=t)
            elif kind == "reroute":
                nav.reroute({"points": record["points"]}, name=record.get("name"), t=t)
            elif kind == "floor":
                nav.next_floor(t)
            elif kind == "calibrate":
                nav.calibrate(t)
            elif kind == "start":
                nav.start(t)
            elif kind == "stop":
                nav.stop(t)
            elif kind == "stale":
                nav.on_stale(t)
            elif kind == "wearer":
                nav.set_wearer_height(record["height_cm"], t)
            elif kind == "mark":
                nav.mark(record["waypoint"], t)
            elif kind == "obstacle_mode":
                nav.set_obstacle_mode(record["enabled"], t)
            elif kind == "obstacle":
                nav.on_obstacle(record.get("left_m"), record.get("ahead_m"), record.get("right_m"), t)
            elif kind == "sensor":
                result.samples += 1
                nav.on_sample(SensorSample(record["t"], record["heading_deg"], record["step_count"], record.get("raw")))
        except (NavError, KeyError) as exc:
            result.errors.append(f"{kind} at t={t}: {exc}")

    logged_outputs = [r for r in records if r.get("type") not in INPUT_TYPES]
    replayed_outputs = [r for r in out.records if r.get("type") not in INPUT_TYPES]
    result.logged_taps = [_tap(r) for r in logged_outputs if r["type"] == "tap"]
    result.replayed_taps = [_tap(r) for r in replayed_outputs if r["type"] == "tap"]
    for name, bucket in ((result.logged_events, logged_outputs), (result.replayed_events, replayed_outputs)):
        name.update(r["type"] for r in bucket if r["type"] not in ("tap", "pose"))

    logged_poses = [r for r in logged_outputs if r["type"] == "pose"]
    replayed_poses = [r for r in replayed_outputs if r["type"] == "pose"]
    result.poses_compared = min(len(logged_poses), len(replayed_poses))
    result.pose_mismatches = abs(len(logged_poses) - len(replayed_poses))
    for a, b in zip(logged_poses, replayed_poses):
        error = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
        result.max_pose_error_m = max(result.max_pose_error_m, error)
        if error > POSE_TOLERANCE or a.get("state") != b.get("state") or a.get("target") != b.get("target"):
            result.pose_mismatches += 1

    result.final_state = nav.follower.state.value
    if nav.pose is not None:
        result.final_pose = (nav.pose.x, nav.pose.y, nav.pose.heading_deg)
    return result


def replay_file(path: str | Path, cfg: Config | None = None) -> ReplayResult:
    return replay(read_log(Path(path)), cfg)
