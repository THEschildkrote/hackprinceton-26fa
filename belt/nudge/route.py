"""Route input, in a flat building frame: metres, x to the right, y up.

Accepted shapes:

    {"waypoints": [[x, y], ...]}                 this runtime's own format
    {"points": [[x, y, floor], ...]}             a planner's route
    {"route": {"points": [[x, y, floor], ...]}}  the upstream planner's /api/route answer, as it is

A third element in a point is its floor. A route that changes floor is split
there: each floor's stretch is walked as a route of its own, and the walk
waits at the change until it is told the wearer is on the next floor.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

Waypoints = list[tuple[float, float]]

_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class RouteError(ValueError):
    pass


_SHAPE = 'route must be an object like {"waypoints": [[x_m, y_m], ...]} or {"points": [[x_m, y_m, floor], ...]}'


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def extract_floors(data: Any) -> list[tuple[Any, list[list[float]]]]:
    """The route from any accepted shape, split where the floor changes:
    [(floor, [[x, y], ...]), ...] in walking order, before any flipping or
    simplifying. A point without a third element has floor None."""
    if isinstance(data, dict) and isinstance(data.get("route"), dict):
        data = data["route"]
    if not isinstance(data, dict):
        raise RouteError(_SHAPE)
    raw = data["waypoints"] if "waypoints" in data else data.get("points")
    if raw is None:
        raise RouteError(_SHAPE)
    if not isinstance(raw, list):
        raise RouteError("waypoints must be a list")
    floors: list[tuple[Any, list[list[float]]]] = []
    for i, item in enumerate(raw):
        if not (isinstance(item, (list, tuple)) and len(item) in (2, 3) and _number(item[0]) and _number(item[1])):
            raise RouteError(f"waypoint {i} must be [x_m, y_m] or [x_m, y_m, floor] with finite numbers")
        floor = item[2] if len(item) == 3 else None
        if not floors or floor != floors[-1][0]:
            floors.append((floor, []))
        floors[-1][1].append([float(item[0]), float(item[1])])
    return floors


def extract_points(data: Any) -> tuple[list[list[float]], int]:
    """The first floor's stretch as plain [[x, y], ...], and how many points
    lie beyond the first change of floor."""
    floors = extract_floors(data)
    if not floors:
        return [], 0
    return floors[0][1], sum(len(points) for _, points in floors[1:])


MIN_LEG_M = 0.3  # a first leg shorter than this is not worth turning the wearer for


def join_route(here: tuple[float, float], waypoints: Waypoints, reach_m: float) -> Waypoints:
    """A route to carry on with from where the wearer is standing.

    If they are within reach_m of it, it is picked up at the nearest place:
    the points already behind them are dropped and the first leg runs from
    where they are to the next point. Further away than that, the first leg
    takes them to the start of the route."""
    hx, hy = here
    nearest, index = math.inf, 0
    for i in range(len(waypoints) - 1):
        (ax, ay), (bx, by) = waypoints[i], waypoints[i + 1]
        dx, dy = bx - ax, by - ay
        u = min(max(((hx - ax) * dx + (hy - ay) * dy) / (dx * dx + dy * dy), 0.0), 1.0)
        d = math.hypot(hx - (ax + u * dx), hy - (ay + u * dy))
        if d < nearest - 1e-9:
            nearest, index = d, i
    rest = list(waypoints[index + 1:] if nearest <= reach_m else waypoints)
    while len(rest) > 1 and math.dist(here, rest[0]) < MIN_LEG_M:
        rest.pop(0)
    if not rest or math.dist(here, rest[0]) < 1e-6:
        raise RouteError("the wearer is already at the end of that route")
    return [(float(hx), float(hy))] + rest


def simplify(points: Waypoints, tolerance_m: float) -> Waypoints:
    """Drop points that lie within tolerance_m of the line through their
    neighbours (Douglas-Peucker). A planner's route threads through scan
    positions and wiggles by a few decimetres; followed literally, every
    wiggle is a tap. Corners further out than the tolerance are kept."""
    if tolerance_m <= 0 or len(points) < 3:
        return list(points)
    (ax, ay), (bx, by) = points[0], points[-1]
    length = math.hypot(bx - ax, by - ay)
    worst, index = -1.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if length < 1e-9:
            d = math.hypot(px - ax, py - ay)
        else:
            d = abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / length
        if d > worst:
            worst, index = d, i
    if worst <= tolerance_m:
        return [points[0], points[-1]]
    return simplify(points[: index + 1], tolerance_m)[:-1] + simplify(points[index:], tolerance_m)


def finish_route(points: list[list[float]], flip_y: bool = False, simplify_m: float = 0.0) -> Waypoints:
    """Extracted points to the waypoints that are followed."""
    out: Waypoints = []
    for x, y in points:
        point = (float(x), -float(y) if flip_y else float(y))
        if out and math.dist(out[-1], point) < 1e-6:
            continue  # repeated point: no leg to walk
        out.append(point)
    if simplify_m > 0 and len(out) > 2:
        # The first leg is kept exactly as given: calibration is "standing on
        # the first point, facing the second", so it must be the leg the
        # wearer was told to face.
        out = [out[0]] + simplify(out[1:], simplify_m)
    if len(out) < 2:
        raise RouteError("a route needs at least two distinct waypoints")
    return out


def parse_route(data: Any, flip_y: bool = False, simplify_m: float = 0.0) -> Waypoints:
    return finish_route(extract_points(data)[0], flip_y=flip_y, simplify_m=simplify_m)


def route_path(routes_dir: Path, name: str) -> Path:
    stem = name[:-5] if name.endswith(".json") else name
    if not _NAME.match(stem):
        raise RouteError(f"bad route name: {name!r}")
    path = routes_dir / f"{stem}.json"
    if not path.is_file():
        raise RouteError(f"no such route: {stem}")
    return path


def save_route_file(routes_dir: Path, name: str, points: list[list[Any]]) -> Path:
    """Write a route into routes_dir as <name>.json, as it was given (every
    floor, not flipped or simplified), so loading it by name later is the
    same as being sent it again. Names that start with "demo" are the routes
    that ship with the runtime and are not overwritten."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise RouteError("a route name is letters, digits, - and _ only")
    if name.lower().startswith("demo"):
        raise RouteError('names starting with "demo" are the built-in routes: pick another')
    if len(points) < 2:
        raise RouteError("no route is loaded")
    one_floor = len({p[2] for p in points}) == 1
    data = ({"name": name, "waypoints": [[p[0], p[1]] for p in points]} if one_floor
            else {"name": name, "points": [list(p) for p in points]})
    routes_dir.mkdir(parents=True, exist_ok=True)
    path = routes_dir / f"{name}.json"
    tmp = routes_dir / f".{name}.json.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise RouteError(f"cannot write {path}: {exc}") from exc
    return path


def read_route_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError(f"cannot read route {path}: {exc}") from exc


def list_routes(routes_dir: Path) -> list[str]:
    if not routes_dir.is_dir():
        return []
    return sorted(p.stem for p in routes_dir.glob("*.json"))
