"""turns any matterport model (panos + wall graph + rooms) into a walkable graph.

nodes are scan positions: someone stood there with a tripod, so they are walkable.
two panos on a floor are linked when the straight line between them crosses no wall.
floors are joined through rooms labelled stairs or elevator.
"""
import json
from dataclasses import dataclass, field

from .geom import dist, point_in_poly, poly_len, project, segments_cross
from .mp import ROOT

# open_plan: a room boundary with no wall around it; walkable, but not a door
PASSABLE = {"opening", "door", "doorway", "entrance", "entry", "archway", "open", "passage", "open_plan"}
NOT_DOORS = {"open_plan"}
DOORWAYS = ROOT / "cache" / "doorways"   # doorways found in the point cloud, per scan
DOOR_MIN_M = 0.5     # a free run narrower than this along a wall is noise or a corner, not a doorway
OPEN_PLAN_M = 2.5    # wider than this is an open-plan boundary, not a door
STAIRS = ("stair", "staircase", "stairwell", "steps")
ELEVATOR = ("elevator", "lift")
MAX_LINK = 5.0      # metres between linked panos
MAX_LINK_LONELY = 8.0
K_NEAREST = 6
VERTICAL_XY = 5.0
BRIDGE = 4.0        # bridge disconnected pieces across one wall, flagged uncertain
DOOR_APPROACH = 0.5  # cross a doorway square-on: a point this far before and after its middle
CLEAR_M = 0.35       # keep the walking line this far off blocking walls
DOOR_ADA_M = 0.70    # hard stop for wheelchairs (most manual chairs are 0.63-0.69 m); ada asks 0.815 m clear
DOOR_NARROW_M = 0.9  # narrower than this gets called out as a narrow doorway


@dataclass
class Node:
    id: str
    label: str
    x: float
    y: float
    z: float
    floor: str
    room: str | None = None
    room_label: str | None = None
    tags: set = field(default_factory=set)
    landmarks: list = field(default_factory=list)

    @property
    def xy(self):
        return (self.x, self.y)


@dataclass
class Edge:
    a: str
    b: str
    length: float
    tags: set = field(default_factory=set)
    hazards: list = field(default_factory=list)
    via: list = field(default_factory=list)    # waypoints between a and b, in a->b order
    doors: list = field(default_factory=list)  # [{x, y, width}] doorways this link goes through, a->b order
    min_clear: float | None = None             # closest approach to a blocking wall, metres
    info: dict = field(default_factory=dict)   # e.g. a portal's kind, note and target map

    def other(self, n):
        return self.b if n == self.a else self.a


class WalkGraph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.adj: dict[str, dict[str, Edge]] = {}
        self.floors: list[dict] = []      # [{id, label, walls: [((x,y),(x,y),type)]}]
        self.rooms: dict[str, dict] = {}  # id -> {label, floor, poly}
        self.warnings: list[str] = []

    def add_node(self, n):
        self.nodes[n.id] = n
        self.adj.setdefault(n.id, {})

    def link(self, a, b, tags=()):
        if b in self.adj[a]:
            self.adj[a][b].tags |= set(tags)
            return self.adj[a][b]
        e = Edge(a, b, dist(self.nodes[a].xy, self.nodes[b].xy), set(tags))
        self.adj[a][b] = self.adj[b][a] = e
        return e

    def edges(self):
        seen = set()
        for a, nb in self.adj.items():
            for b, e in nb.items():
                if id(e) not in seen:
                    seen.add(id(e))
                    yield e

    def edge(self, a, b):
        return self.adj[a].get(b)

    def edge_points(self, a, b):
        """the walking polyline from node a to node b, door and clearance waypoints included."""
        e = self.adj[a][b]
        pts = [self.nodes[e.a].xy, *[tuple(p) for p in e.via], self.nodes[e.b].xy]
        return pts if a == e.a else pts[::-1]

    def edge_doors(self, a, b):
        e = self.adj[a][b]
        return e.doors if a == e.a else e.doors[::-1]

    def frame(self, floor):
        """coordinate frame of a floor; floors of different scans can't be compared metrically."""
        f = next((f for f in self.floors if f["id"] == floor), None)
        return (f or {}).get("frame", "")

    def room_at(self, x, y, floor):
        """smallest labelled room polygon containing the point, or None."""
        hits = [(k, r) for k, r in self.rooms.items()
                if r["floor"] == floor and r["poly"] and len(r["poly"]) >= 3 and point_in_poly((x, y), r["poly"])]
        hits.sort(key=lambda kr: (kr[1]["label"] in (None, "Void"), _area(kr[1]["poly"])))
        return hits[0][0] if hits else None

    def walls(self, floor):
        f = next((f for f in self.floors if f["id"] == floor), None)
        return f["walls"] if f else []

    def blocked_line(self, p, q, floor):
        """first blocking wall the segment pq crosses, or None."""
        for w in self.walls(floor):
            if w[2] not in PASSABLE and segments_cross(p, q, w[0], w[1]):
                return w
        return None

    def door_crossings(self, p, q, floor):
        return [w for w in self.walls(floor) if w[2] in PASSABLE and segments_cross(p, q, w[0], w[1])]

    def components(self):
        comp, cid = {}, 0
        for s in self.nodes:
            if s in comp:
                continue
            stack = [s]
            comp[s] = cid
            while stack:
                u = stack.pop()
                for v in self.adj[u]:
                    if v not in comp:
                        comp[v] = cid
                        stack.append(v)
            cid += 1
        return comp

    def to_dict(self):
        return {
            "floors": [{"id": f["id"], "label": f["label"],
                        "walls": [[list(a), list(b), t] for a, b, t in f["walls"]]} for f in self.floors],
            "rooms": {k: {"label": r["label"], "floor": r["floor"], "poly": [list(p) for p in r["poly"]]}
                      for k, r in self.rooms.items() if r["poly"]},
            "nodes": [{"id": n.id, "label": n.label, "x": n.x, "y": n.y, "z": n.z, "floor": n.floor,
                       "room": n.room, "room_label": n.room_label, "tags": sorted(n.tags),
                       "landmarks": n.landmarks} for n in self.nodes.values()],
            "edges": [{"a": e.a, "b": e.b, "length": round(e.length, 3), "tags": sorted(e.tags),
                       "hazards": e.hazards, "via": [list(p) for p in e.via], "doors": e.doors}
                      for e in self.edges()],
            "warnings": self.warnings,
        }


def _label(room):
    c = room.get("classifications") or []
    return c[0].get("label") if c else None


def _room_poly(room, V, E):
    edges = [E[e["id"]] for e in (room.get("boundary") or {}).get("edges") or [] if e["id"] in E]
    if not edges:
        return None
    loop, rest = list(edges[0]), edges[1:]
    while rest:
        for i, (a, b) in enumerate(rest):
            if a == loop[-1] or b == loop[-1]:
                loop.append(b if a == loop[-1] else a)
                rest.pop(i)
                break
        else:
            break
    return [V[v] for v in loop if v in V]


def _assign_floors_by_z(panos, floor_ids):
    """when panos don't say their floor: split sorted z at the largest gaps."""
    if len(floor_ids) <= 1:
        return {p["id"]: floor_ids[0] if floor_ids else "f0" for p in panos}
    zs = sorted(panos, key=lambda p: p["position"]["z"])
    gaps = sorted(range(1, len(zs)), key=lambda i: zs[i]["position"]["z"] - zs[i - 1]["position"]["z"],
                  reverse=True)[:len(floor_ids) - 1]
    cuts, out, k = sorted(gaps), {}, 0
    for i, p in enumerate(zs):
        while k < len(cuts) and i >= cuts[k]:
            k += 1
        out[p["id"]] = floor_ids[k]
    return out


def _is(label, words):
    return bool(label) and any(w in label.lower() for w in words)


def build(model, annotations=None, use_doorways=True):
    """model: the model api `model {...}` dict (see fetch.Q_WALK). annotations: the agent dossier, optional.
    use_doorways: apply cache/doorways/<id>.json (point-cloud doorways) when it exists."""
    g = WalkGraph()
    floors = sorted(model.get("floors") or [], key=lambda f: (f.get("sequence") is None, f.get("sequence") or 0))
    floor_ids = [f["id"] for f in floors]
    E_all = {}
    for f in floors:
        V = {v["id"]: (v["position"]["x"], v["position"]["y"]) for v in f.get("vertices") or []}
        E = {e["id"]: [v["id"] for v in e["vertices"]] for e in f.get("edges") or []}
        E_all.update({k: (v, V) for k, v in E.items()})
        walls = [(V[a], V[b], (e.get("type") or "wall").lower())
                 for e in f.get("edges") or [] for a, b in [[v["id"] for v in e["vertices"]][:2]]
                 if a in V and b in V]
        g.floors.append({"id": f["id"], "label": f.get("label") or f["id"], "walls": walls})

    # real scans type every edge "wall"; doorways found in the point cloud split those walls
    dw = load_doorways(model.get("id")) if use_doorways else None
    if dw:
        for f in g.floors:
            f["walls"] = split_walls(f["walls"], dw.get("floors", {}).get(f["id"], []))

    for r in model.get("rooms") or []:
        fid = (r.get("floor") or {}).get("id")
        edges = (r.get("boundary") or {}).get("edges") or []
        V = E_all[edges[0]["id"]][1] if edges and edges[0]["id"] in E_all else {}
        E = {k: v for k, (v, _) in E_all.items()}
        g.rooms[r["id"]] = {"label": _label(r), "floor": fid, "poly": _room_poly(r, V, E)}

    panos = [p for p in model.get("panoLocations") or [] if p.get("position")]
    by_z = _assign_floors_by_z(panos, floor_ids) if any(not (p.get("floor") or {}).get("id") for p in panos) else {}
    for p in panos:
        pos = p["position"]
        fid = (p.get("floor") or {}).get("id") or by_z.get(p["id"]) or "f0"
        n = Node(p["id"], p.get("label") or p["id"], pos["x"], pos["y"], pos.get("z", 0.0), fid)
        rid = (p.get("room") or {}).get("id")
        if not rid:
            hits = [(k, r) for k, r in g.rooms.items()
                    if r["floor"] == fid and r["poly"] and len(r["poly"]) >= 3 and point_in_poly(n.xy, r["poly"])]
            hits.sort(key=lambda kr: (kr[1]["label"] == "Void", _area(kr[1]["poly"])))
            rid = hits[0][0] if hits else None
        if rid in g.rooms:
            n.room, n.room_label = rid, g.rooms[rid]["label"]
        if _is(n.room_label, STAIRS):
            n.tags.add("stairs")
        if _is(n.room_label, ELEVATOR):
            n.tags.add("elevator")
        g.add_node(n)

    if not g.floors:
        g.floors = [{"id": fid, "label": fid, "walls": []} for fid in sorted({n.floor for n in g.nodes.values()})]
        g.warnings.append("no wall graph: links are by distance only")

    _link_same_floor(g)
    gap_doorways(g)
    for a, nbrs in (model.get("neighbors") or {}).items():  # showcase sdk sweep graph, when we have it
        for b in nbrs:
            if a in g.nodes and b in g.nodes:
                g.link(a, b, ["sdk"])
    _link_floors(g, [f["id"] for f in g.floors])
    _bridge(g)
    shape_edges(g)
    if annotations:
        apply_annotations(g, annotations)
    return g


def _area(poly):
    return abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1] for i in range(len(poly)))) / 2


def _link_same_floor(g):
    by_floor = {}
    for n in g.nodes.values():
        by_floor.setdefault(n.floor, []).append(n)
    for fid, ns in by_floor.items():
        for a in ns:
            cands = sorted(((dist(a.xy, b.xy), b) for b in ns if b.id != a.id), key=lambda t: t[0])
            made = 0
            for limit in (MAX_LINK, MAX_LINK_LONELY):
                for d, b in cands:
                    if d > limit or made >= K_NEAREST:
                        break
                    if b.id in g.adj[a.id] or g.blocked_line(a.xy, b.xy, fid):
                        continue
                    tags = ["door"] if g.door_crossings(a.xy, b.xy, fid) else []
                    if a.room and b.room and a.room != b.room:
                        tags.append("room_change")
                    g.link(a.id, b.id, tags)
                    made += 1
                if g.adj[a.id]:
                    break


def _link_floors(g, floor_ids):
    for lo, hi in zip(floor_ids, floor_ids[1:]):
        A = [n for n in g.nodes.values() if n.floor == lo]
        B = [n for n in g.nodes.values() if n.floor == hi]
        made = False
        for kind in ("elevator", "stairs"):
            for a in A:
                if kind not in a.tags:
                    continue
                near = [b for b in B if kind in b.tags and dist(a.xy, b.xy) <= VERTICAL_XY]
                if near:
                    b = min(near, key=lambda b: dist(a.xy, b.xy))
                    e = g.link(a.id, b.id, [kind, "vertical"])
                    e.length = abs(b.z - a.z) + dist(a.xy, b.xy)
                    made = True
        if not made and A and B:
            a, b = min(((a, b) for a in A for b in B), key=lambda ab: dist(ab[0].xy, ab[1].xy))
            e = g.link(a.id, b.id, ["vertical", "vertical_unknown"])
            e.length = abs(b.z - a.z) + dist(a.xy, b.xy)
            g.warnings.append(f"no stairs/elevator room between {lo} and {hi}: guessed link {a.label}-{b.label}")


def _bridge(g):
    """join stray pieces to the main one through the shortest crossing <= BRIDGE m, marked uncertain."""
    while True:
        comp = g.components()
        sizes = {}
        for c in comp.values():
            sizes[c] = sizes.get(c, 0) + 1
        if len(sizes) <= 1:
            return
        main = max(sizes, key=sizes.get)
        best = None
        for a in g.nodes.values():
            if comp[a.id] == main:
                continue
            for b in g.nodes.values():
                if comp[b.id] == main and a.floor == b.floor:
                    d = dist(a.xy, b.xy)
                    if d <= BRIDGE and (best is None or d < best[0]):
                        best = (d, a, b)
        if not best:
            stray = [n.label for n in g.nodes.values() if comp[n.id] != main]
            g.warnings.append(f"unreachable panos: {', '.join(stray[:12])}{'...' if len(stray) > 12 else ''}")
            return
        _, a, b = best
        g.link(a.id, b.id, ["uncertain"])
        g.warnings.append(f"bridged {a.label}-{b.label} across a wall (no clear line of sight)")


def _seg_clear(p, q, walls, skip_pts, step=0.2, end_skip=0.25, door_skip=0.6):
    """(closest distance to a blocking wall along pq, sample point, wall). ignores the ends (panos) and
    the stretch right at a doorway, where the jambs are close by design."""
    L = dist(p, q)
    n = max(1, int(L / step))
    best = (float("inf"), None, None)
    for i in range(n + 1):
        t = i / n
        s = (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
        if t * L < end_skip and p in skip_pts["ends"] or (1 - t) * L < end_skip and q in skip_pts["ends"]:
            continue
        if any(dist(s, d) < door_skip for d in skip_pts["doors"]):
            continue
        for w in walls:
            d = project(s, w[0], w[1])[2]
            if d < best[0]:
                best = (d, s, w)
    return best


def _near_walls(g, floor, p, q, pad):
    lo_x, hi_x = min(p[0], q[0]) - pad, max(p[0], q[0]) + pad
    lo_y, hi_y = min(p[1], q[1]) - pad, max(p[1], q[1]) + pad
    return [w for w in g.walls(floor) if w[2] not in PASSABLE
            and max(w[0][0], w[1][0]) >= lo_x and min(w[0][0], w[1][0]) <= hi_x
            and max(w[0][1], w[1][1]) >= lo_y and min(w[0][1], w[1][1]) <= hi_y]


def _clear_path(g, floor, pts):
    return all(not g.blocked_line(a, b, floor) for a, b in zip(pts, pts[1:]))


def _door_via(g, floor, a, b):
    """waypoints that take the line a->b through the middle of each doorway it crosses, square-on."""
    crossings = sorted((w for w in g.door_crossings(a, b, floor) if w[2] not in NOT_DOORS),
                       key=lambda w: project(((w[0][0] + w[1][0]) / 2, (w[0][1] + w[1][1]) / 2), a, b)[0])
    via, doors = [], []
    for w in crossings:
        mid = ((w[0][0] + w[1][0]) / 2, (w[0][1] + w[1][1]) / 2)
        width = dist(w[0], w[1])
        # shared walls show up once per room; one doorway, one set of waypoints
        if width < 1e-6 or any(dist(mid, (d["x"], d["y"])) < 0.1 for d in doors):
            continue
        doors.append({"x": round(mid[0], 3), "y": round(mid[1], 3), "width": round(width, 3)})
        ux, uy = -(w[1][1] - w[0][1]) / width, (w[1][0] - w[0][0]) / width  # wall normal
        if ux * (b[0] - a[0]) + uy * (b[1] - a[1]) < 0:
            ux, uy = -ux, -uy
        pre = (mid[0] - ux * DOOR_APPROACH, mid[1] - uy * DOOR_APPROACH)
        post = (mid[0] + ux * DOOR_APPROACH, mid[1] + uy * DOOR_APPROACH)
        prev = via[-1] if via else a
        pts = ([pre] if dist(prev, mid) > DOOR_APPROACH + 0.2 else []) + [mid] +               ([post] if dist(mid, b) > DOOR_APPROACH + 0.2 else [])
        via += pts
    # keep the square-on approach only if it stays clear; else just the middle; else the straight line
    for cand in (via, [p for p in via if any(abs(p[0] - d["x"]) < 1e-9 and abs(p[1] - d["y"]) < 1e-9
                                             for d in doors)], []):
        if _clear_path(g, floor, [a, *cand, b]):
            return cand, doors
    return [], doors


def shape_edges(g):
    """give every walking link a precise polyline: through doorway middles, off the walls."""
    for e in g.edges():
        na, nb = g.nodes[e.a], g.nodes[e.b]
        if na.floor != nb.floor or "vertical" in e.tags or "portal" in e.tags:
            continue
        floor = na.floor
        via, doors = _door_via(g, floor, na.xy, nb.xy)
        pts = [na.xy, *via, nb.xy]
        skip = {"ends": {na.xy, nb.xy}, "doors": [(d["x"], d["y"]) for d in doors]}
        out, worst = [pts[0]], float("inf")
        for p, q in zip(pts, pts[1:]):
            walls = _near_walls(g, floor, p, q, CLEAR_M + 0.1)
            d, s, w = _seg_clear(p, q, walls, skip) if walls else (float("inf"), None, None)
            if d < CLEAR_M and s is not None:
                c = project(s, w[0], w[1])[1]
                ln = dist(s, c)
                if ln > 1e-6:
                    push = CLEAR_M - d + 0.05
                    s2 = (s[0] + (s[0] - c[0]) / ln * push, s[1] + (s[1] - c[1]) / ln * push)
                    w1 = _near_walls(g, floor, p, s2, CLEAR_M + 0.1)
                    w2 = _near_walls(g, floor, s2, q, CLEAR_M + 0.1)
                    d2 = min(_seg_clear(p, s2, w1, skip)[0] if w1 else float("inf"),
                             _seg_clear(s2, q, w2, skip)[0] if w2 else float("inf"))
                    if _clear_path(g, floor, [p, s2, q]) and d2 > d:
                        out.append(s2)
                        d = d2
            worst = min(worst, d)
            out.append(q)
        e.via = [tuple(round(c, 3) for c in pt) for pt in out[1:-1]]
        e.doors = doors
        e.min_clear = None if worst == float("inf") else round(worst, 3)
        e.length = poly_len(out)
        if worst < CLEAR_M:
            e.tags.add("tight")
        if any(d["width"] < DOOR_ADA_M for d in doors):
            e.tags.add("narrow_door")
        if doors:
            e.tags.add("door")


# doorways. typed scans mark them (door/opening edges). real matterport scans type every edge "wall":
# a doorway is then either a gap between wall ends, or a "wall" edge that is really a room separator
# with nothing physical in it. the point cloud tells the two apart: jamb, empty run, jamb.

def load_doorways(sid):
    p = DOORWAYS / f"{sid}.json" if sid else None
    if not p or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["floors"] = apply_review(sid, data.get("floors") or {})
    return data


def apply_review(sid, floors):
    """drop doorways a visual review marked not_a_door (cache/doorways/<sid>.review.json):
    a railing over a stair void or a recessed panel can look like an empty run in the cloud."""
    p = DOORWAYS / f"{sid}.review.json" if sid else None
    if not p or not p.exists():
        return floors
    try:
        rev = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return floors
    drops = [(r["floor"], r["mid"]) for r in rev.get("doors", []) if r.get("verdict") == "not_a_door"]
    out = {}
    for fid, ds in floors.items():
        keep = []
        for d in ds:
            mid = ((d["a"][0] + d["b"][0]) / 2, (d["a"][1] + d["b"][1]) / 2)
            if any(f == fid and dist(mid, m) < 0.3 for f, m in drops):
                continue
            keep.append(d)
        out[fid] = keep
    return out


def split_walls(walls, doors):
    """cut each doorway span out of the wall it lies on; doorways on no wall are added as they are."""
    out, used = [], set()
    for p, q, kind in walls:
        L = dist(p, q)
        cuts = []
        for i, d in enumerate(doors):
            a, b = tuple(d["a"]), tuple(d["b"])
            ta, _, da = project(a, p, q)
            tb, _, db = project(b, p, q)
            if L > 1e-6 and da < 0.06 and db < 0.06 and abs(ta - tb) * L > 0.2:
                cuts.append((min(ta, tb), max(ta, tb), d))
                used.add(i)
        if not cuts:
            out.append((p, q, kind))
            continue
        lerp = lambda t: (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
        t0 = 0.0
        for s, e, d in sorted(cuts, key=lambda c: c[0]):
            if (s - t0) * L > 0.02:
                out.append((lerp(t0), lerp(s), kind))
            out.append((lerp(max(s, t0)), lerp(e), d.get("kind", "door")))
            t0 = max(t0, e)
        if (1 - t0) * L > 0.02:
            out.append((lerp(t0), q, kind))
    out += [(tuple(d["a"]), tuple(d["b"]), d.get("kind", "door")) for i, d in enumerate(doors) if i not in used]
    return out


def gap_doorways(g):
    """links between rooms that cross no wall pass through a gap: measure it between the nearest
    collinear wall ends and add it as a door (or an open-plan boundary when no walls bound it)."""
    for e in list(g.edges()):
        a, b = g.nodes[e.a], g.nodes[e.b]
        if "room_change" not in e.tags or a.floor != b.floor or g.door_crossings(a.xy, b.xy, a.floor):
            continue
        poly = (g.rooms.get(a.room) or {}).get("poly")
        if not poly or len(poly) < 3:
            continue
        hit = None
        for i in range(len(poly)):
            u, v = poly[i - 1], poly[i]
            if segments_cross(a.xy, b.xy, u, v):
                hit = (u, v)
                break
        if not hit:
            continue
        u, v = hit
        L = dist(u, v)
        if L < 1e-6:
            continue
        ux, uy = (v[0] - u[0]) / L, (v[1] - u[1]) / L
        # where the link crosses the boundary line, as a distance along it
        c = _line_cross(a.xy, b.xy, u, v)
        if c is None:
            continue
        tc = (c[0] - u[0]) * ux + (c[1] - u[1]) * uy
        lo, hi = None, None
        for p, q, kind in g.walls(a.floor):
            if kind in PASSABLE:
                continue
            for w in (p, q):
                off = abs((w[0] - u[0]) * -uy + (w[1] - u[1]) * ux)
                if off > 0.15:
                    continue
                t = (w[0] - u[0]) * ux + (w[1] - u[1]) * uy
                if t <= tc and (lo is None or t > lo):
                    lo = t
                if t >= tc and (hi is None or t < hi):
                    hi = t
        if lo is not None and hi is not None and hi - lo <= OPEN_PLAN_M and hi - lo >= DOOR_MIN_M * 0.6:
            kind, t0, t1 = "door", lo, hi
        else:
            kind, t0, t1 = "open_plan", tc - OPEN_PLAN_M / 2, tc + OPEN_PLAN_M / 2
        seg = ((u[0] + ux * t0, u[1] + uy * t0), (u[0] + ux * t1, u[1] + uy * t1), kind)
        f = next(f for f in g.floors if f["id"] == a.floor)
        if not any(dist(s[0], seg[0]) < 0.05 and dist(s[1], seg[1]) < 0.05 for s in f["walls"]):
            f["walls"].append(seg)


def _line_cross(p, q, u, v):
    d1x, d1y, d2x, d2y = q[0] - p[0], q[1] - p[1], v[0] - u[0], v[1] - u[1]
    den = d1x * d2y - d1y * d2x
    if abs(den) < 1e-12:
        return None
    t = ((u[0] - p[0]) * d2y - (u[1] - p[1]) * d2x) / den
    return (p[0] + t * d1x, p[1] + t * d1y)


def _grow_run(hc, cthr, k, direction, nb, max_bins):
    """walk out from a run edge until a jamb (points crossing the wall's centre plane). a door leaf
    standing in the slab looks solid but sits off the centre plane, so it gets absorbed into the run.
    returns the new edge bin, or None when no jamb is found within reach (then leave the run alone)."""
    for steps in range(max_bins + 1):
        if not 0 <= k < nb:
            return None
        if hc[k] >= cthr:
            return k
        k += direction
    return None


def cloud_doorways(g, pts, levels, step=0.03, half=0.15, band=(0.15, 1.9), core=0.03, reach=0.3):
    """{floor_id: [doorway...]} from the point cloud: along each wall edge, a run of empty slices
    between jambs in the band a person walks through. pts is (n, 3) in the model frame.
    widths are clear widths, jamb face to jamb face (what ada 404.2.3 measures)."""
    import numpy as np
    out = {}
    for f in g.floors:
        if f["id"] not in levels:
            continue
        z0 = levels[f["id"]][0]
        sel = pts[(pts[:, 2] > z0 + band[0]) & (pts[:, 2] < z0 + band[1])][:, :2]
        found = []
        for p, q, kind in f["walls"]:
            if kind in PASSABLE:
                continue
            L = dist(p, q)
            if L < DOOR_MIN_M:
                continue
            P = np.array(p)
            u = (np.array(q) - P) / L
            nrm = np.array([-u[1], u[0]])
            lo, hi = np.minimum(p, q) - 0.3, np.maximum(p, q) + 0.3
            box = sel[(sel[:, 0] > lo[0]) & (sel[:, 0] < hi[0]) & (sel[:, 1] > lo[1]) & (sel[:, 1] < hi[1])]
            rel = box - P
            t_all, s_all = rel @ u, rel @ nrm
            t = t_all[np.abs(s_all) < half]
            nb = max(1, int(round(L / step)))
            h, _ = np.histogram(t, bins=nb, range=(0, L))
            # jambs cross the wall's centre plane; wall faces and door leaves mostly don't
            hc, _ = np.histogram(t_all[np.abs(s_all) < core], bins=nb, range=(0, L))
            core_solid = hc[hc > 0]
            cthr = max(2, 0.25 * float(np.median(core_solid))) if len(core_solid) else 2
            max_bins = int(round(reach / (L / nb)))
            solid = h[h > 0]
            # a doorway is near-empty (scan noise leaves ~10% of wall density); walls stay dense
            thr = max(3, 0.25 * float(np.median(solid))) if len(solid) else 3
            free = h <= thr
            i = 0
            while i < nb:
                if not free[i]:
                    i += 1
                    continue
                j = i
                while j < nb and free[j]:
                    j += 1
                run_end = j
                # a leaf standing in the opening: grow the run out to the real jambs
                lo = _grow_run(hc, cthr, i - 1, -1, nb, max_bins) if i > 0 else None
                hi = _grow_run(hc, cthr, j, 1, nb, max_bins) if j < nb else None
                if lo is not None:
                    i = lo + 1
                if hi is not None:
                    j = hi
                w = (j - i) * L / nb
                at_end = i == 0 or j == nb
                # a free run at the very end of an edge is usually a corner, unless it is most of the edge
                if w >= DOOR_MIN_M and (not at_end or w >= 0.9 * L):
                    a, b = P + u * (i * L / nb), P + u * (j * L / nb)
                    found.append({"a": [round(float(a[0]), 3), round(float(a[1]), 3)],
                                  "b": [round(float(b[0]), 3), round(float(b[1]), 3)],
                                  "width": round(w, 3), "kind": "door" if w <= OPEN_PLAN_M else "open_plan",
                                  "exterior": _is_exterior(g, f["id"], p, q), "source": "cloud"})
                i = max(j, run_end)
        out[f["id"]] = found
    return out


def _is_exterior(g, floor, p, q):
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    L = dist(p, q)
    if L < 1e-6:
        return False
    nx, ny = -(q[1] - p[1]) / L, (q[0] - p[0]) / L
    sides = [g.room_at(mx + nx * 0.4, my + ny * 0.4, floor), g.room_at(mx - nx * 0.4, my - ny * 0.4, floor)]
    return sum(1 for s in sides if s is None) == 1


def compute_doorways(sid, model=None, pts=None, save=True):
    """find doorways in the scan's point cloud and cache them; later builds of this scan use them."""
    from . import perceive
    from .fetch import load
    model = model or load(sid)
    g = build(model, use_doorways=False)
    pts = perceive.cloud(sid) if pts is None else pts
    levels = perceive.floor_levels(g, pts)
    floors = apply_review(sid, cloud_doorways(g, pts, levels))
    res = {"sid": sid, "floors": floors,
           "stats": {fid: {"doors": sum(1 for d in ds if d["kind"] == "door"),
                           "open_plan": sum(1 for d in ds if d["kind"] == "open_plan"),
                           "exterior": sum(1 for d in ds if d["exterior"]),
                           "widths": sorted(d["width"] for d in ds if d["kind"] == "door")}
                     for fid, ds in floors.items()}}
    if save:
        DOORWAYS.mkdir(parents=True, exist_ok=True)
        (DOORWAYS / f"{sid}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def _poly_dist(g, e, xy):
    pts = g.edge_points(e.a, e.b)
    return min(project(xy, a, b)[2] for a, b in zip(pts, pts[1:]))


def apply_annotations(g, ann):
    """the dossier format the offline agent writes:
    {"nodes": {pano_id: {"tags": [...], "landmarks": [{"text":..., "sense": "touch|sound|underfoot|air"}]}},
     "edges": {"a|b": {"tags": [...]}},
     "hazards": [{"x":..,"y":..,"floor":..,"radius":..,"kind":"protruding_object","note":"..."}]}
    """
    for nid, info in (ann.get("nodes") or {}).items():
        if nid in g.nodes:
            g.nodes[nid].tags |= set(info.get("tags") or [])
            g.nodes[nid].landmarks += info.get("landmarks") or []
    for key, info in (ann.get("edges") or {}).items():
        a, _, b = key.partition("|")
        e = g.edge(a, b) if a in g.adj else None
        if e:
            e.tags |= set(info.get("tags") or [])
    for h in ann.get("hazards") or []:
        for e in g.edges():
            na, nb = g.nodes[e.a], g.nodes[e.b]
            if na.floor != h.get("floor", na.floor) or "vertical" in e.tags:
                continue
            if _poly_dist(g, e, (h["x"], h["y"])) <= h.get("radius", 0.6):
                e.tags.add("hazard")
                e.hazards.append(h)
        # edges the geometry candidate was found on, even if a bit farther than radius
        for a, b in h.get("on_edges") or (h.get("evidence") or {}).get("on_edges") or []:
            e = g.edge(a, b) if a in g.adj else None
            if e and h not in e.hazards:
                e.tags.add("hazard")
                e.hazards.append(h)
