"""live position from a phone's step count and compass, kept on the walk graph.

dead reckoning in open space drifts a metre every few steps. a blind traveller following a route
is almost always on it, so we keep them there: steps move them along the route polyline, and the
compass only decides when they have clearly gone another way.

- heading within 50 deg of the route: steps advance along the route.
- heading 50-135 deg off for 3+ steps: they took another edge. move along the graph edge that best
  matches the heading; the navigator sees them leave the route and reroutes.
- heading more than 135 deg off for 3+ steps: they turned around. walk them back along the route.
- floor changes: at a stairs link, 16 steps on the stairs moves them to the next floor; at an
  elevator, 10 s standing still then a step out does. the phone can also send floor_confirm.

    python -m nudge.pdr --replay walk.jsonl --sid MgGNVWsyXtH --to Kitchen --dry
    python -m nudge.pdr --make-walk walk.jsonl --sid MgGNVWsyXtH --to Kitchen   # a clean walk to replay
"""
import argparse
import json
import math
import time

from .geom import dist, heading as heading_of, turn_angle
from .instruct import meters

STEP_M = 0.7
OFF_DEG = 50          # heading this far off the route counts as "not following it"
BACK_DEG = 135        # this far off: facing back the way they came
OFF_STEPS = 3         # steps of sustained disagreement before we believe the compass
EDGE_DEG = 60         # free mode: how well an edge must match the heading to take it
STAIRS_STEPS = 16     # about one flight
ELEVATOR_DWELL_S = 10
DIST_MARKS = (20, 10, 5)
DIST_GAP_S = 3.0


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def compass_from_model(geo, h):
    """inverse of georef.heading_model_from_compass, in compass degrees."""
    d = math.degrees(h)
    c = 90.0 - geo.theta_deg + d if geo.mirrored else 90.0 - geo.theta_deg - d
    return c % 360


class Tracker:
    """one traveller's position on one navigator. call calibrate() once at the start, step() per batch."""

    def __init__(self, nav, geo=None, step_m=STEP_M):
        self.nav, self.geo, self.step_m = nav, geo, step_m
        self.corr = 0.0 if geo is not None else None   # model = base(compass) + corr
        self.off_steps = 0
        self.mode = "route"
        self.free_to = None        # free mode: the node we're walking toward
        self.pending = None        # (path index of the vertical point, kind, arrived_t, steps_since)
        self.said = set()
        self.last_say_t = -1e9
        self._sync_route()

    # --- frames ---------------------------------------------------------------

    def _base(self, compass):
        if self.geo is not None:
            return self.geo.heading_model_from_compass(compass)
        return math.radians(-compass)   # sense only; calibrate supplies the offset

    def to_model(self, compass):
        if compass is None or self.corr is None:
            return None
        return _wrap(self._base(compass) + self.corr)

    def route_heading(self, along=None):
        """model heading of the walking polyline at `along` (looks a little ahead past corners)."""
        P = self.nav.path
        a = (self.along if along is None else along) + 0.3
        for j in range(1, len(P)):
            p, q = P[j - 1], P[j]
            if q[5] or dist(p[:2], q[:2]) < 1e-6:
                continue
            if q[3] >= a or j == len(P) - 1:
                return heading_of(p[:2], q[:2])
        return None

    def calibrate(self, compass):
        """standing at the start, facing the way the first instruction says."""
        r = self.route_heading(0.0)
        if r is None:
            return {"ok": False, "why": "no route"}
        self.corr = _wrap(r - self._base(compass))
        return {"ok": True, "correction_deg": round(math.degrees(self.corr), 1),
                "model_heading_deg": round(math.degrees(r), 1)}

    def start_compass(self):
        """compass heading to face for the first instruction, when the building is on the map."""
        r = self.route_heading(0.0)
        return None if (r is None or self.geo is None) else round(compass_from_model(self.geo, r), 1)

    # --- polyline helpers -------------------------------------------------------

    def _sync_route(self):
        """after a (re)route: take up the navigator's position on its new polyline."""
        self.route_id = id(self.nav.route)
        self.along = getattr(self.nav, "progress", 0.0) if self.nav.route else 0.0
        self.mode, self.free_to, self.pending, self.off_steps = "route", None, None, 0
        P = self.nav.path if self.nav.route else []
        if P:
            self.pos = self._point(self.along)[:3]
        elif self.nav.pos:
            self.pos = tuple(self.nav.pos[:3])
        else:
            self.pos = None

    def _point(self, along):
        """(x, y, floor, j) on the polyline at `along`, where j is the segment it lies in."""
        P = self.nav.path
        if along <= 0 or len(P) < 2:
            return (P[0][0], P[0][1], P[0][2], 1)
        for j in range(1, len(P)):
            p, q = P[j - 1], P[j]
            if q[3] >= along:
                if q[5] or q[3] - p[3] < 1e-9:
                    return (p[0], p[1], p[2], j)
                t = (along - p[3]) / (q[3] - p[3])
                return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]), q[2], j)
        q = P[-1]
        return (q[0], q[1], q[2], len(P) - 1)

    def _advance(self, d, now):
        """move forward d metres along the route, stopping at a floor change."""
        P = self.nav.path
        target = self.along + d
        for j in range(1, len(P)):
            p, q = P[j - 1], P[j]
            if q[3] <= self.along:
                continue
            if q[5]:  # floor link: wait at its foot
                self.along = p[3]
                kind = self._link_kind(q[4])
                if not self.pending or self.pending[0] != j:
                    self.pending = [j, kind, now, 0]
                break
            if q[3] >= target:
                self.along = target
                break
            self.along = q[3]
        self.pos = self._point(self.along)[:3]

    def _link_kind(self, edge_index):
        r = self.nav.route
        a, b = r.nodes[edge_index], r.nodes[edge_index + 1]
        tags = self.nav.g.edge(a, b).tags
        return "elevator" if "elevator" in tags else "stairs"

    def _cross_floor(self):
        j = self.pending[0]
        q = self.nav.path[j]
        self.along, self.pos, self.pending = q[3], (q[0], q[1], q[2]), None

    # --- free walking on the graph ------------------------------------------------

    def _free(self, h, d):
        """walk d metres along graph edges that match heading h, starting from wherever we are."""
        g = self.nav.g
        x, y, floor = self.pos
        while d > 1e-6:
            if self.free_to is None:
                if h is None:
                    return  # no compass this tick: can't pick an edge, wait for the next heading
                near = min((n for n in g.nodes.values() if n.floor == floor),
                           key=lambda n: dist((x, y), n.xy), default=None)
                if near is None:
                    return
                best = None
                # from the nearest pano, or toward it if it's ahead of us
                for nb, e in list(g.adj[near.id].items()) + [(near.id, None)]:
                    tn = g.nodes[nb]
                    if "vertical" in getattr(e, "tags", ()) or tn.floor != floor or dist((x, y), tn.xy) < 0.3:
                        continue
                    dev = abs(math.degrees(_wrap(heading_of((x, y), tn.xy) - h)))
                    if dev < EDGE_DEG and (best is None or dev < best[0]):
                        best = (dev, nb)
                if best is None:
                    return  # nothing goes that way: stay put rather than walk into a wall
                self.free_to = best[1]
            tn = g.nodes[self.free_to]
            gap = dist((x, y), tn.xy)
            if gap <= d:
                x, y, d = tn.x, tn.y, d - gap
                self.free_to = None
            else:
                t = d / gap
                x, y, d = x + t * (tn.x - x), y + t * (tn.y - y), 0.0
        self.pos = (x, y, floor)

    # --- the one call per update -----------------------------------------------------

    def step(self, steps=0, compass=None, step_m=None, floor_confirm=False, now=None):
        now = time.monotonic() if now is None else now
        nav = self.nav
        if nav.route is not None and id(nav.route) != self.route_id:
            self._sync_route()
        h = self.to_model(compass)
        d = max(0, int(steps)) * (step_m or self.step_m)
        if nav.route is not None and self.pos is not None and not nav.done:
            if self.pending is not None:
                self.pending[3] += max(0, int(steps))
                j, kind, t0, n = self.pending
                if floor_confirm or (kind == "stairs" and n >= STAIRS_STEPS) or \
                        (kind == "elevator" and now - t0 >= ELEVATOR_DWELL_S and steps > 0):
                    self._cross_floor()
            elif d > 0:
                r = self.route_heading()
                dev = None if (h is None or r is None) else abs(math.degrees(_wrap(h - r)))
                if self.mode == "free":
                    self._free(h, d)
                elif dev is not None and dev > OFF_DEG:
                    # the compass disagrees with the route: hold still until it has for a few steps
                    self.off_steps += int(steps)
                    if self.off_steps >= OFF_STEPS:
                        if dev > BACK_DEG:
                            self.along = max(0.0, self.along - d)   # turned around: retrace
                            self.pos = self._point(self.along)[:3]
                        else:
                            self.mode = "free"   # took another way: follow the graph, the navigator reroutes
                            self._free(h, d)
                else:
                    self.off_steps = 0
                    self._advance(d, now)
        x, y, floor = self.pos if self.pos else (None, None, None)
        events = nav.update(x, y, floor, h) if self.pos else []
        if nav.route is not None and id(nav.route) != self.route_id:
            self._sync_route()   # the navigator rerouted from where we are: follow the new route
        lv = nav.live(h)
        say = [e["text"] for e in events if e.get("text")]
        line = self._distance_line(lv, events, now)
        if line:
            say.append(line)
        return {"events": events, "say": say, "position": {"x": x, "y": y, "floor": floor},
                "distance_to_next_m": lv.get("distance_to_next_m"), "bearing_to_next_deg": lv.get("bearing_to_next_deg"),
                "done": nav.done, "heading_model": h, "mode": self.mode,
                "waiting_for": self.pending[1] if self.pending else None}

    def _distance_line(self, lv, events, now):
        """a short metres line when the distance crosses 20/10/5 m, never on top of a turn cue."""
        to_next, nxt = lv.get("distance_to_next_m"), lv.get("next")
        if to_next is None or not nxt or self.nav.done or any(e["type"] in ("now", "soon", "leg") for e in events):
            return None
        mark = next((m for m in DIST_MARKS if to_next <= m and to_next > m - 4), None)
        key = (self.nav.mi, mark)
        if mark is None or key in self.said or now - self.last_say_t < DIST_GAP_S:
            return None
        self.said.add(key)
        self.last_say_t = now
        kind = nxt.get("type")
        what = "to the next turn" if kind in ("turn", "bear", "uturn") else \
            f"to the {nxt.get('via') or 'stairs'}" if kind in ("floor", "walk") else "to go"
        return f"{meters(to_next)} {what}."


# --- replay: drive a session from recorded {t, steps, heading_deg} lines -------------------

def make_walk(tracker, path, cadence_s=0.55, wobble_deg=8):
    """a clean recorded walk along the route: one step per line, compass headings that follow it."""
    import random
    rnd = random.Random(0)
    out, t, along = [], 0.0, 0.0
    P = tracker.nav.path
    total = P[-1][3] if P else 0
    links = [(P[j - 1][3], P[j][3], tracker._link_kind(P[j][4])) for j in range(1, len(P)) if P[j][5]]
    while along < total + tracker.step_m:
        link = next((lk for lk in links if lk[0] - 1e-6 <= along < lk[1]), None)
        if link:  # on the stairs: a flight of steps; in the elevator: stand still, then step out
            if link[2] == "stairs":
                for _ in range(STAIRS_STEPS):
                    out.append({"t": round(t, 2), "steps": 1, "heading_deg": None})
                    t += cadence_s
            else:
                t += ELEVATOR_DWELL_S + 1
            along = link[1]
            continue
        r = tracker.route_heading(along)
        c = None
        if r is not None:
            base = (r - tracker.corr) if tracker.corr is not None else r
            c = (-math.degrees(base) + rnd.uniform(-wobble_deg, wobble_deg)) % 360 if tracker.geo is None else \
                compass_from_model(tracker.geo, r + math.radians(rnd.uniform(-wobble_deg, wobble_deg)))
        out.append({"t": round(t, 2), "steps": 1, "heading_deg": None if c is None else round(c, 1)})
        along += tracker.step_m
        t += cadence_s
    with open(path, "w", encoding="utf-8") as f:
        for line in out:
            f.write(json.dumps(line) + "\n")
    return len(out)


_SITE_JOBS = {}


def site_for(sid, wait_s=3.0):
    """the building's outdoor site (georef, entrances) if it can be had quickly; None otherwise.
    a first build can take minutes (usgs elevation), so it keeps loading in the background and a trip
    never waits on it; the next trip picks it up."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    try:
        from .handoff import _SITES, load_site
    except Exception:
        return None
    if sid in _SITES:
        return _SITES[sid]
    job = _SITE_JOBS.get(sid)
    if job is None or (job.done() and job.exception() is not None):
        job = _SITE_JOBS[sid] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"site-{sid}").submit(load_site, sid)
    try:
        return job.result(timeout=wait_s)
    except FutTimeout:
        return None
    except Exception:
        return None


def resolve_start(g, sid, start=None, site=None):
    """'entrance' (default), a place name, or a pano id/label -> a node on the graph."""
    from .agents.tools import key_places
    s = (start or "entrance").strip()
    if s in g.nodes:
        return g.nodes[s]
    by_label = next((n for n in g.nodes.values() if n.label == s), None)
    if by_label:
        return by_label
    if s.lower() in ("entrance", "the entrance", "front door", "door"):
        site = site if site is not None else site_for(sid)
        try:
            p = site.best_portal() if site else None
            if p and p["pano_id"] in g.nodes:
                return g.nodes[p["pano_id"]]
        except Exception:
            pass
        return key_places(g)[0]
    from . import places
    r = places.resolve(g, s)
    if r.get("best"):
        ids = places.goal_nodes(r["best"]) if hasattr(places, "goal_nodes") else []
        ids = [i for i in ids if i in g.nodes]
        if ids:
            return g.nodes[ids[0]]
    raise ValueError(f"I don't know where '{start}' is in this building")


def _setup(sid, goal, start, profile):
    from .agents.tools import graph
    from .navigator import Navigator
    g = graph(sid)
    site = site_for(sid)
    node = resolve_start(g, sid, start, site)
    nav = Navigator(g, goal, profile)
    nav.start(node.x, node.y, node.floor)
    return nav, Tracker(nav, geo=site.geo if site else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="drive the navigator (and the collar) from recorded steps + compass")
    ap.add_argument("--replay")
    ap.add_argument("--make-walk")
    ap.add_argument("--sid", default="MgGNVWsyXtH")
    ap.add_argument("--to", default="Kitchen")
    ap.add_argument("--start", default="entrance")
    ap.add_argument("--profile", default="blind")
    ap.add_argument("--dry", action="store_true", help="no gpio: record the band instead of moving servos")
    ap.add_argument("--fast", action="store_true", help="don't wait real time between lines")
    a = ap.parse_args(argv)
    nav, tr = _setup(a.sid, a.to, a.start, a.profile)
    if tr.corr is None:
        tr.calibrate(0.0)   # recorded walks without a georef are made against compass 0 = route start
    if a.make_walk:
        print(f"wrote {make_walk(tr, a.make_walk)} lines to {a.make_walk}")
        return
    if not a.replay:
        ap.error("--replay or --make-walk")
    from .band import NullBand, ServoBand
    from .bandrun import BandRunner
    band = NullBand() if a.dry else ServoBand()
    runner = BandRunner(nav, band=band)
    t0 = time.monotonic()
    lines = [json.loads(s) for s in open(a.replay, encoding="utf-8") if s.strip()]
    for rec in lines:
        due = rec.get("t", 0.0)
        while not a.fast and time.monotonic() - t0 < due:
            runner.tick(int((time.monotonic() - t0) * 1000))
            time.sleep(0.005)
        now = due if a.fast else time.monotonic() - t0
        out = tr.step(rec.get("steps", 0), rec.get("heading_deg"), now=now)
        runner.sync(out["heading_model"], int(now * 1000))
        for _ in range(80 if a.fast else 0):   # let a stroke render in fast mode
            now += 0.005
            runner.tick(int(now * 1000))
        for s in out["say"]:
            print(f"[{now:6.1f}s] {s}")
        if out["done"]:
            break
    changes = len(getattr(band, "commands", []))
    runner.close()
    print(f"done={nav.done} reroutes={nav.reroutes}" + (f" servo state changes={changes}" if a.dry else ""))


if __name__ == "__main__":
    main()
