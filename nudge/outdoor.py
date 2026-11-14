"""outdoor walk graph around a building, in local east/north metres (origin = the model's geolocation).

topology: openstreetmap footways, sidewalks and crossings (overpass api), because vexcel's /elements/detect is
open-vocabulary detection (polygons for a text label), not a routable network.
vexcel is the sensor layer: footprint (destination), dtm slope per edge, curb steps, detected obstacles and
crosswalks, and oblique facades for entrance checks (handoff.py).
the graph uses walkgraph's Node/Edge/WalkGraph with floor "outdoor", so router/navigator/instruct work unchanged.
"""
import hashlib
import json
import math

import requests

from . import vexcel
from .geom import project
from .mp import ROOT
from .router import PROFILES, Profile
from .walkgraph import Node, WalkGraph, apply_annotations

FLOOR = "outdoor"
SAMPLE_M = 2.0        # dtm samples along each edge
STEEP = 0.05          # 1:20, ada 403.3 max running slope for a walking surface
TOO_STEEP = 1 / 12    # 1:12, ada 405.2 max ramp slope (and only with handrails)
CURB_STEP_M = 0.10    # a dtm jump this big over one sample, with no curb ramp known, is a curb
CURB_GRADE = 0.12     # ...if it is also sharper than any ramp
MIN_GRADE_RUN_M = 1.5  # shorter edges are too short to judge a slope from the dtm
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRRORS = ("https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
OSM_CACHE = ROOT / "cache" / "osm"
USER_AGENT = "nudge-wayfinding-prototype/0.1 (hackathon accessibility research)"
WALKABLE = ("footway", "path", "pedestrian", "steps", "living_street", "crossing", "sidewalk", "corridor")
ROADS = ("residential", "service", "unclassified", "tertiary", "secondary", "primary")
OBSTACLE_LABELS = ("pole", "bollard", "tree trunk", "bench", "fire hydrant", "planter")
CROSSWALK_LABEL = "crosswalk"
CURB_RAMP_LABEL = "curb ramp"
# streets that get sidewalks inferred on both sides when osm doesn't map them (not alleys, not motorways)
SIDEWALK_ROADS = ("primary", "secondary", "tertiary", "residential", "unclassified")
HALF_WIDTH = {"primary": 6.0, "secondary": 5.0, "tertiary": 4.5, "residential": 4.0, "unclassified": 3.5}
LANE_M = 3.3
SIDEWALK_GAP_M = 1.5    # curb to the middle of the sidewalk
DENSIFY_M = 10.0        # sidewalk nodes at least this often, so gps snaps nearby
SIGNAL_NEAR_M = 20.0    # a signal node this close to a junction makes its crossings signalised
CONNECT_M = 8.0         # dangling footway ends join the nearest inferred sidewalk within this

# tests swap this: overpass_transport(query) -> dict (overpass json)
OVERPASS_TRANSPORT = None

# extra penalties for outdoor tags, merged over the indoor profiles
OUTDOOR = {
    "default": {"avoid": set(), "penalty": {"roadway": 10, "crossing": 5, "unsignalised": 5, "steep": 5, "curb": 5,
                                            "too_steep": 15, "sidewalk_inferred": 1, "driveway_crossing": 1}},
    # blind: crossings and curbs are the risk; slopes barely matter
    "blind": {"avoid": set(), "penalty": {"roadway": 60, "crossing": 25, "unsignalised": 40, "curb": 30,
                                          "steep": 0.5, "too_steep": 2, "sidewalk_inferred": 1,
                                          "driveway_crossing": 5}},
    # wheelchair: a curb or a ramp steeper than 1:12 is a hard stop
    "wheelchair": {"avoid": {"too_steep", "curb"}, "penalty": {"roadway": 40, "crossing": 5, "unsignalised": 10,
                                                               "steep": 20, "sidewalk_inferred": 1,
                                                               "driveway_crossing": 2}},
}


def profile(name):
    """indoor profile plus outdoor penalties, as a router.Profile you can hand to plan() or Navigator."""
    base = PROFILES[name]
    extra = OUTDOOR.get(name, OUTDOOR["default"])
    return Profile(name, set(base.avoid) | extra["avoid"], {**base.penalty, **extra["penalty"]}, base.turn_cost)


# osm

def overpass(lat, lon, radius_m=150):
    """footways, sidewalks, crossings, steps, and nearby streets, with crossing and kerb nodes."""
    q = f"""[out:json][timeout:25];
(
  way(around:{radius_m},{lat},{lon})[highway~"^({'|'.join(WALKABLE + ROADS)})$"];
  node(around:{radius_m},{lat},{lon})[highway=crossing];
  node(around:{radius_m},{lat},{lon})[barrier=kerb];
  node(around:{radius_m},{lat},{lon})[kerb];
  node(around:{radius_m},{lat},{lon})[entrance];
);
out body geom;"""
    return _overpass_query(q)


def signal_nodes(lat, lon, radius_m=150):
    """traffic signal nodes (on the road ways themselves, so the main query doesn't return their tags)."""
    q = f"""[out:json][timeout:25];
(
  node(around:{radius_m},{lat},{lon})[highway=traffic_signals];
  node(around:{radius_m},{lat},{lon})[crossing=traffic_signals];
);
out body;"""
    return _overpass_query(q)


def _overpass_query(q):
    if OVERPASS_TRANSPORT is not None:
        return OVERPASS_TRANSPORT(q)
    key = hashlib.sha1(q.encode()).hexdigest()[:20]
    p = OSM_CACHE / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    # the main overpass server is often overloaded (502/504/429); try the public mirrors in turn
    last = None
    for url in (OVERPASS, *OVERPASS_MIRRORS):
        try:
            r = requests.post(url, data={"data": q}, headers={"User-Agent": USER_AGENT}, timeout=90)
            if r.status_code in (429, 502, 503, 504):
                last = requests.HTTPError(f"{r.status_code} from {url}")
                continue
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as ex:
            last = ex
            continue
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text, encoding="utf-8")
        return data
    raise last


def _edge_tags(way_tags):
    t = set()
    hw = way_tags.get("highway", "")
    if hw in ROADS:
        t.add("roadway")  # walking along a street edge: no mapped sidewalk
    if hw == "steps":
        t.add("stairs")
    if hw == "crossing" or way_tags.get("footway") == "crossing" or way_tags.get("crossing"):
        t.add("crossing")
        c = way_tags.get("crossing", "")
        t.add("signals" if c in ("traffic_signals", "signals") or way_tags.get("crossing:signals") == "yes"
              else "unsignalised")
    return t


def _kerb_kind(tags):
    """'curb' (a step), 'ramp' (lowered/flush), or None."""
    k = tags.get("kerb")
    if k in ("raised", "rolled", "regular", "yes") or (tags.get("barrier") == "kerb" and not k):
        return "curb"
    if k in ("lowered", "flush", "no"):
        return "ramp"
    return None


def build(lat, lon, osm=None, elevation=None, detections=None, footprint_lonlat=None, radius_m=150):
    """outdoor WalkGraph around (lat, lon). osm: overpass json (fetched if None); elevation: fn([(lon,lat)]) -> [z]
    (vexcel dtm if None); detections: vexcel /elements/detect results (fetched if None, skipped on error)."""
    from .georef import ENU
    enu = ENU(lat, lon)
    osm = osm if osm is not None else overpass(lat, lon, radius_m)
    elevation = elevation or vexcel.elevation
    g = WalkGraph()
    g.floors = [{"id": FLOOR, "label": "Outside", "walls": [], "frame": "outdoor"}]
    g.enu = enu
    g.elevation = elevation
    g.footprint = [enu.to_enu(x, y) for x, y in footprint_lonlat] if footprint_lonlat else None

    nodes_info = {el["id"]: el.get("tags") or {} for el in osm.get("elements", []) if el.get("type") == "node"}
    crossing_nodes = {nid: t for nid, t in nodes_info.items() if t.get("highway") == "crossing"}
    kerbs = {nid: _kerb_kind(t) for nid, t in nodes_info.items() if _kerb_kind(t)}
    g.roads, road_ways = [], []
    # what a crossing node crosses: a street (inferred-sidewalk road) or just a driveway/alley (service road)
    street_nodes, service_nodes = set(), set()
    for el in osm.get("elements", []):
        if el.get("type") == "way":
            t = el.get("tags") or {}
            if t.get("highway") in SIDEWALK_ROADS:
                street_nodes.update(el.get("nodes") or [])
            elif t.get("highway") == "service":
                service_nodes.update(el.get("nodes") or [])
    mapped_sidewalk = set()  # nodes of osm-mapped sidewalks, joined to the inferred ones later

    def crossing_tags(raw):
        if raw in service_nodes and raw not in street_nodes:
            return {"driveway_crossing"}  # a sidewalk across a driveway mouth, not a street
        c = crossing_nodes[raw].get("crossing", "")
        return {"crossing", "signals" if c in ("traffic_signals", "signals") else "unsignalised"}

    def nid(osm_id):
        return f"out:{osm_id}"

    for el in osm.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags") or {}
        ids, geom = el.get("nodes") or [], el["geometry"]
        if tags.get("highway") in ROADS:
            # every street, kept for "which side faces the main road" even when it isn't walked on
            g.roads.append({"pts": [enu.to_enu(p["lon"], p["lat"]) for p in geom], "highway": tags.get("highway"),
                            "name": tags.get("name"), "osm_way": el["id"]})
        if _infer_sidewalk(tags) and len(ids) == len(geom):
            road_ways.append((el, tags))  # walked on inferred sidewalks, not down the middle of the street
            continue
        base = _edge_tags(tags)
        for i, gpt in enumerate(geom):
            k = nid(ids[i]) if i < len(ids) else nid(f"{el['id']}_{i}")
            if k not in g.nodes:
                x, y = enu.to_enu(gpt["lon"], gpt["lat"])
                g.add_node(Node(k, tags.get("name") or k, x, y, 0.0, FLOOR))
        for i in range(len(geom) - 1):
            a = nid(ids[i]) if i < len(ids) else nid(f"{el['id']}_{i}")
            b = nid(ids[i + 1]) if i + 1 < len(ids) else nid(f"{el['id']}_{i + 1}")
            if a == b:
                continue
            tg = set(base)
            # crossing info carried on the crossing node, but only for the footway that crosses there:
            # walking along the road itself past a crossing node is not crossing anything
            if tags.get("highway") not in ROADS:
                for raw in (ids[i] if i < len(ids) else None, ids[i + 1] if i + 1 < len(ids) else None):
                    if raw in crossing_nodes:
                        tg |= crossing_tags(raw)
            if tags.get("footway") == "sidewalk" or tags.get("highway") == "sidewalk":
                mapped_sidewalk.update({a, b})
            e = g.link(a, b, tg)
            e.info.update({"osm_way": el["id"], "highway": tags.get("highway")})
            for raw in (ids[i] if i < len(ids) else None, ids[i + 1] if i + 1 < len(ids) else None):
                if kerbs.get(raw) == "curb":
                    e.tags.add("curb")
                elif kerbs.get(raw) == "ramp":
                    e.info["curb_ramp"] = True
    # osm entrance nodes (entrance=main/yes/...) sit on building outlines: strong hint for the handoff
    for raw, t in nodes_info.items():
        if t.get("entrance") and nid(raw) in g.nodes:
            g.nodes[nid(raw)].tags.add("entrance")
            g.nodes[nid(raw)].landmarks.append({"text": f"{t.get('entrance')} entrance", "sense": "touch"})
    if road_ways:
        signals = [enu.to_enu(el["lon"], el["lat"]) for el in osm.get("elements", [])
                   if el.get("type") == "node" and "lat" in el and (el.get("tags") or {}).get("crossing") == "traffic_signals"]
        try:
            sig = signal_nodes(lat, lon, radius_m)
            signals += [enu.to_enu(el["lon"], el["lat"]) for el in sig.get("elements", [])
                        if el.get("type") == "node" and "lat" in el
                        and ((el.get("tags") or {}).get("highway") == "traffic_signals"
                             or (el.get("tags") or {}).get("crossing") == "traffic_signals")]
        except Exception as ex:  # overpass down: crossings default to unsignalised, the cautious choice
            g.warnings.append(f"traffic signals unknown ({ex}); inferred crossings treated as unsignalised")
        _infer_sidewalks(g, road_ways, crossing_nodes, signals, nid, mapped_sidewalk)
    # signalised beats unsignalised when both got added
    for e in g.edges():
        if "signals" in e.tags:
            e.tags.discard("unsignalised")
    _slopes(g, elevation)
    if detections is None:
        try:
            detections = vexcel.detect(lat, lon, list(OBSTACLE_LABELS) + [CROSSWALK_LABEL, CURB_RAMP_LABEL],
                                       half_m=radius_m)
        except Exception as ex:  # no key, quota, outside coverage: routing still works without it
            g.warnings.append(f"vexcel detect skipped: {ex}")
            detections = []
    _apply_detections(g, detections)
    return g


def _infer_sidewalk(tags):
    """a street we should walk beside rather than in: sidewalk-class road, no 'separate' or 'no' sidewalk tag."""
    if tags.get("highway") not in SIDEWALK_ROADS:
        return False
    vals = {tags.get(k) for k in ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right")}
    if "separate" in vals:
        return False  # mapped as their own footways; those are used instead
    return not (vals & {"no", "none"}) or bool(vals & {"both", "left", "right", "yes"})


def _offset_m(tags):
    """centreline to the middle of the sidewalk: half the carriageway plus the curb gap."""
    try:
        return float(str(tags.get("width", "")).split()[0]) / 2 + SIDEWALK_GAP_M
    except (ValueError, IndexError):
        pass
    try:
        return int(tags.get("lanes")) * LANE_M / 2 + SIDEWALK_GAP_M
    except (TypeError, ValueError):
        return HALF_WIDTH.get(tags.get("highway"), 4.0) + SIDEWALK_GAP_M


def _infer_sidewalks(g, road_ways, crossing_nodes, signals, nid, mapped_sidewalk=()):
    """sidewalk polylines on both sides of streets that osm draws only as centrelines. corners join the
    sidewalks of one block; crossings exist only at junctions (or at osm-mapped crossing nodes)."""
    xy, arms, off, name = {}, {}, {}, {}
    for el, tags in road_ways:
        ids = el["nodes"]
        for raw, p in zip(ids, el["geometry"]):
            xy[raw] = g.enu.to_enu(p["lon"], p["lat"])
        for a, b in zip(ids, ids[1:]):
            if a == b or math.dist(xy[a], xy[b]) < 0.5:
                continue
            for u, v in ((a, b), (b, a)):
                arms.setdefault(u, []).append(v)
                off[(u, v)] = _offset_m(tags)
                name[(u, v)] = tags.get("name") or "the street"

    def unit(u, v):
        (ux, uy), (vx, vy) = xy[u], xy[v]
        L = math.hypot(vx - ux, vy - uy)
        return (vx - ux) / L, (vy - uy) / L, L

    def end(u, v, side):
        """sidewalk node near u on the left/right of the arm u->v (looking from u toward v)."""
        k = f"sw:{u}:{v}:{side}"
        if k not in g.nodes:
            dx, dy, L = unit(u, v)
            d = off[(u, v)]
            s = 1 if side == "L" else -1
            pull = min(d, L / 2) if len(arms[u]) >= 3 else 0.0  # leave room for the corner at a junction
            x, y = xy[u][0] - s * dy * d + dx * pull, xy[u][1] + s * dx * d + dy * pull
            lab = name[(u, v)]
            g.add_node(Node(k, f"{lab} sidewalk" if lab != "the street" else "sidewalk", x, y, 0.0, FLOOR,
                            tags={"sidewalk"}))
        return k

    def link(a, b, tags, info):
        """link a-b, adding nodes every DENSIFY_M so a gps fix always has a sidewalk node nearby."""
        na, nb = g.nodes[a], g.nodes[b]
        L = math.dist(na.xy, nb.xy)
        n = max(1, math.ceil(L / DENSIFY_M))
        prev = a
        for i in range(1, n):
            k = f"{a}~{b}:{i}"
            g.add_node(Node(k, na.label, na.x + (nb.x - na.x) * i / n, na.y + (nb.y - na.y) * i / n, 0.0, FLOOR,
                            tags={"sidewalk"}))
            e = g.link(prev, k, tags)
            e.info.update(info)
            prev = k
        e = g.link(prev, b, tags)
        e.info.update(info)

    def signal_state(u):
        return "signals" if any(math.dist(xy[u], s) <= SIGNAL_NEAR_M for s in signals) else "unsignalised"

    side_tags = {"sidewalk_inferred"}
    # along each street, both sides
    done = set()
    for u, vs in arms.items():
        for v in vs:
            if (v, u) in done:
                continue
            done.add((u, v))
            info = {"highway": "sidewalk_inferred", "road": name[(u, v)]}
            link(end(u, v, "L"), end(v, u, "R"), side_tags, info)
            link(end(u, v, "R"), end(v, u, "L"), side_tags, info)
    # corners and crossings at every street vertex
    for u, vs in arms.items():
        order = sorted(set(vs), key=lambda v: math.atan2(xy[v][1] - xy[u][1], xy[v][0] - xy[u][0]))
        n = len(order)
        if n == 1:  # dead end: walking round the end means crossing the street
            e = g.link(end(u, order[0], "L"), end(u, order[0], "R"), {"crossing", "sidewalk_inferred", signal_state(u)})
            e.info.update({"highway": "crossing_inferred", "cross_road": name[(u, order[0])]})
            continue
        for k in range(n):
            a, b = order[k], order[(k + 1) % n]
            e = g.link(end(u, a, "L"), end(u, b, "R"), side_tags | {"corner"})
            e.info.update({"highway": "sidewalk_inferred", "road": name[(u, a)]})
        own_crossing = nid(u) in g.nodes and any("crossing" in e.tags for e in g.adj[nid(u)].values())
        if n >= 3 or (u in crossing_nodes and not own_crossing):
            # a junction (or an osm-mapped crossing with no crossing footway): cross each arm near u
            state = signal_state(u) if n >= 3 else \
                ("signals" if crossing_nodes[u].get("crossing") in ("traffic_signals", "signals") else "unsignalised")
            for v in (order if n >= 3 else order[:1]):
                e = g.link(end(u, v, "L"), end(u, v, "R"), {"crossing", "sidewalk_inferred", state})
                e.info.update({"highway": "crossing_inferred", "cross_road": name[(u, v)]})
    # join the mapped walkable ways to the inferred sidewalks
    sw = [n for n in g.nodes.values() if n.id.startswith("sw:") or "~" in n.id]
    for n in list(g.nodes.values()):
        if not n.id.startswith("out:"):
            continue
        raw = n.id[4:]
        raw = int(raw) if raw.isdigit() else raw
        nbrs = list(g.adj[n.id])
        if raw in xy and raw not in crossing_nodes and nbrs:
            # a service road or path meeting the street: join the sidewalk on its own side
            nb = g.nodes[nbrs[0]]
            L = math.dist(n.xy, nb.xy) or 1.0
            q = (n.x + (nb.x - n.x) / L * 3, n.y + (nb.y - n.y) / L * 3)
            near = [g.nodes[f"sw:{raw}:{v}:{s}"] for v in set(arms[raw]) for s in "LR" if f"sw:{raw}:{v}:{s}" in g.nodes]
            if near:
                tgt = min(near, key=lambda m: math.dist(m.xy, q))
                g.link(n.id, tgt.id, {"connector"}).info.update({"highway": "connector"})
        elif (len(nbrs) == 1 or n.id in mapped_sidewalk) and raw not in xy and sw:
            # a mapped footway that just stops, or a mapped sidewalk running beside an inferred one: join them
            tgt = min(sw, key=lambda m: math.dist(m.xy, n.xy))
            if math.dist(tgt.xy, n.xy) <= CONNECT_M:
                g.link(n.id, tgt.id, {"connector"}).info.update({"highway": "connector"})


def _slopes(g, elevation):
    """sample the dtm along every edge: running slope, steep tags, curb steps."""
    plan = []
    for e in g.edges():
        a, b = g.nodes[e.a], g.nodes[e.b]
        n = max(1, math.ceil(e.length / SAMPLE_M))
        pts = [(a.x + (b.x - a.x) * i / n, a.y + (b.y - a.y) * i / n) for i in range(n + 1)]
        plan.append((e, pts))
    flat = [g.enu.to_lonlat(*p) for _, pts in plan for p in pts]
    if not flat:
        return
    try:
        zs = elevation(flat)
    except Exception as ex:
        g.warnings.append(f"no elevation data: {ex}; slopes unknown")
        return
    k = 0
    for e, pts in plan:
        z = zs[k:k + len(pts)]
        k += len(pts)
        run = e.length / (len(pts) - 1) if len(pts) > 1 else 0.0
        steps = [(abs(z[i + 1] - z[i]), abs(z[i + 1] - z[i]) / run if run > 0 else 0.0) for i in range(len(z) - 1)]
        # a sharp jump over one sample is a curb, not a slope
        curbs = [s for s in steps if s[0] >= CURB_STEP_M and s[1] > CURB_GRADE]
        slopes = [s[1] for s in steps if s not in curbs] or [0.0]
        e.info["grade_max"] = round(max(slopes), 4)
        e.info["grade_mean"] = round(abs(z[-1] - z[0]) / e.length, 4) if e.length else 0.0
        e.info["rise_m"] = round(z[-1] - z[0], 3)  # a -> b
        g.nodes[e.a].z, g.nodes[e.b].z = z[0], z[-1]
        if e.length < MIN_GRADE_RUN_M:
            continue  # a few centimetres of dtm noise over a corner link is not a slope or a curb
        if max(slopes) > TOO_STEEP:
            e.tags.add("too_steep")
        elif max(slopes) > STEEP:
            e.tags.add("steep")
        if curbs and not e.info.get("curb_ramp"):
            e.tags.add("curb")


def _apply_detections(g, dets):
    """obstacles become hazards on nearby edges; crosswalk polygons mark crossings as marked."""
    hazards = []
    for d in dets:
        pts = [g.enu.to_enu(*p) for p in vexcel.geometry_points(d)]
        if not pts:
            continue
        cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        label = (d.get("label") or "").lower()
        if label == CROSSWALK_LABEL:
            for e in g.edges():
                if "crossing" in e.tags and project((cx, cy), g.nodes[e.a].xy, g.nodes[e.b].xy)[2] < 3.0:
                    e.tags.add("marked_crossing")
        elif label == CURB_RAMP_LABEL:
            for e in g.edges():
                if "curb" in e.tags and project((cx, cy), g.nodes[e.a].xy, g.nodes[e.b].xy)[2] < 2.0:
                    e.tags.discard("curb")
                    e.info["curb_ramp"] = True
        elif label:
            hazards.append({"x": cx, "y": cy, "floor": FLOOR, "radius": 0.8, "kind": "other",
                            "note": f"{label} near the path", "confidence": d.get("confidence"),
                            "evidence": {"source": "vexcel /elements/detect"}})
    if hazards:
        apply_annotations(g, {"hazards": hazards})


def nearest_node(g, x, y, max_m=None):
    best = min(g.nodes.values(), key=lambda n: math.dist((x, y), n.xy))
    d = math.dist((x, y), best.xy)
    return (best.id, d) if max_m is None or d <= max_m else (None, d)


def describe(g, route, ms):
    """add what matters outside to each maneuver: crossings, curbs, slopes. distances stay in metres."""
    cum = [0.0]
    for a, b in zip(route.nodes, route.nodes[1:]):
        cum.append(cum[-1] + g.edge(a, b).length)
    prev = 0.0
    for m in ms:
        along = m.get("along", 0.0)
        notes = []
        for i, (a, b) in enumerate(zip(route.nodes, route.nodes[1:])):
            if not (prev - 1e-6 <= cum[i] < along - 1e-6 or (cum[i] < along <= cum[i + 1])):
                continue
            e = g.edge(a, b)
            if "crossing" in e.tags:
                road = (e.info or {}).get("cross_road") or "the street"
                n = f"cross {road} at the signal" if "signals" in e.tags else f"cross {road}, no signal; listen for traffic"
                if n not in notes:
                    notes.append(n)
            if "driveway_crossing" in e.tags and "cross a driveway; listen for cars" not in notes:
                notes.append("cross a driveway; listen for cars")
            if "curb" in e.tags and "curb ahead, step down or up" not in notes:
                notes.append("curb ahead, step down or up")
            if e.info.get("curb_ramp") and "curb ramp" not in notes:
                notes.append("curb ramp")
            rise = e.info.get("rise_m", 0.0) * (1 if a == e.a else -1)
            if "steep" in e.tags or "too_steep" in e.tags:
                n = f"steep slope {'up' if rise > 0 else 'down'}"
                if n not in notes:
                    notes.append(n)
            elif e.info.get("grade_mean", 0) > 0.02 and abs(rise) > 0.05:
                n = f"gentle ramp {'up' if rise > 0 else 'down'}"
                if n not in notes:
                    notes.append(n)
        if notes and m.get("text"):
            m["text"] = m["text"].rstrip(".") + ". " + "; ".join(notes).capitalize() + "."
            m["outdoor_notes"] = notes
        prev = along
    return ms


__all__ = ["build", "overpass", "profile", "describe", "nearest_node", "FLOOR"]
