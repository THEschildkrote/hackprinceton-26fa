"""turns any matterport model (panos + wall graph + rooms) into a walkable graph.

nodes are scan positions: someone stood there with a tripod, so they are walkable.
two panos on a floor are linked when the straight line between them crosses no wall.
floors are joined through rooms labelled stairs or elevator.
"""
from dataclasses import dataclass, field

from .geom import dist, point_in_poly, project, segments_cross

PASSABLE = {"opening", "door", "doorway", "entrance", "entry", "archway", "open", "passage"}
STAIRS = ("stair", "staircase", "stairwell", "steps")
ELEVATOR = ("elevator", "lift")
MAX_LINK = 5.0      # metres between linked panos
MAX_LINK_LONELY = 8.0
K_NEAREST = 6
VERTICAL_XY = 5.0
BRIDGE = 4.0        # bridge disconnected pieces across one wall, flagged uncertain


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
                       "hazards": e.hazards} for e in self.edges()],
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


def build(model, annotations=None):
    """model: the model api `model {...}` dict (see fetch.Q_WALK). annotations: the agent dossier, optional."""
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
    for a, nbrs in (model.get("neighbors") or {}).items():  # showcase sdk sweep graph, when we have it
        for b in nbrs:
            if a in g.nodes and b in g.nodes:
                g.link(a, b, ["sdk"])
    _link_floors(g, [f["id"] for f in g.floors])
    _bridge(g)
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
            if project((h["x"], h["y"]), na.xy, nb.xy)[2] <= h.get("radius", 0.6):
                e.tags.add("hazard")
                e.hazards.append(h)
        # edges the geometry candidate was found on, even if a bit farther than radius
        for a, b in h.get("on_edges") or (h.get("evidence") or {}).get("on_edges") or []:
            e = g.edge(a, b) if a in g.adj else None
            if e and h not in e.hazards:
                e.tags.add("hazard")
                e.hazards.append(h)
