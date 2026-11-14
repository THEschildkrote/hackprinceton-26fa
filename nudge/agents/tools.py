"""tools the dossier agents call. all take sid first. they read the same files the agent writes:
/dossier/<file> in the agent is cache/dossiers/<sid>/<file> on disk.
"""
import csv
import json
import math
import threading
from functools import lru_cache

from langchain_core.tools import tool

from .. import walkgraph
from ..instruct import maneuvers
from ..mp import ROOT
from ..router import NoRoute, plan

DOSSIERS = ROOT / "cache" / "dossiers"
KIT = ROOT / "cache" / "costar_kit"
SENSES = {"underfoot", "touch", "sound", "air", "smell"}
KINDS = {"protruding_object", "glass_wall", "glass_door", "open_stair_underside", "low_overhang",
         "floor_level_change", "drop_off", "column", "other"}
ENTRY_WORDS = ("lobby", "entr", "foyer", "reception", "vestibule")


def dossier_dir(sid):
    d = DOSSIERS / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


_LOAD_LOCK = threading.RLock()


def _model(sid):
    # agents call tools in parallel threads; lazy imports and the cache inside load() aren't thread-safe
    from ..fetch import load
    with _LOAD_LOCK:
        return load(sid)


def read_annotations(sid):
    p = dossier_dir(sid) / "annotations.json"
    if not p.exists():
        return None, None
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as ex:
        return None, f"annotations.json is not valid json: {ex}"


@lru_cache(maxsize=16)
def _graph_cached(sid, ann_key):
    ann = json.loads(ann_key) if ann_key else None
    return walkgraph.build(_model(sid), ann)


GENERIC_ROOMS = {None, "", "Other", "Void", "Unknown"}


def _agent_names(sid):
    """pano id -> the name the agents gave its room/zone (places.json plus per-floor fragments)."""
    d = dossier_dir(sid)
    files = [d / "places.json", *sorted((d / "places").glob("*.json"))] if (d / "places").exists() else [d / "places.json"]
    names = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for p in data.get("places") or []:
            if p.get("kind") in ("room", "zone") and p.get("name"):
                for pid in p.get("panos") or []:
                    names[pid] = p["name"]
    return names


def graph(sid, annotated=True):
    ann = read_annotations(sid)[0] if annotated else None
    g = _graph_cached(sid, json.dumps(ann, sort_keys=True) if ann else "")
    g.sid = sid  # lets the place catalog find this scan's /dossier/places.json
    # matterport often labels rooms "Other"; speak the names the agents gave them instead
    for pid, name in _agent_names(sid).items():
        n = g.nodes.get(pid)
        if n is not None and n.room_label in GENERIC_ROOMS:
            n.room_label = name
    return g


def _j(x):
    return json.dumps(x, indent=1, default=str)


def check_floor(sid, floor):
    """a floor id from another scan (or a guess) must fail loudly, not return an empty list."""
    if not floor:
        return
    floors = [f["id"] for f in graph(sid, annotated=False).floors]
    if floor not in floors:
        raise ValueError(f"unknown floor {floor!r} for sid {sid!r}; this scan's floors are {floors}. "
                         f"Floors of other scans belong to their own dossier runs.")


def _img_blocks(jpeg, meta):
    import base64
    return [{"type": "text", "text": _j(meta)},
            {"type": "image", "base64": base64.b64encode(jpeg).decode(), "mime_type": "image/jpeg"}]


def _index_row(sid):
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["sid"] == sid:
                return r
    return None


def _rep_node(g, room_id):
    """the pano nearest the middle of a room."""
    ns = [n for n in g.nodes.values() if n.room == room_id]
    if not ns:
        return None
    cx, cy = sum(n.x for n in ns) / len(ns), sum(n.y for n in ns) / len(ns)
    return min(ns, key=lambda n: math.hypot(n.x - cx, n.y - cy))


def key_places(g):
    """an entrance-ish start and one pano per labelled room."""
    rooms = {}
    for n in g.nodes.values():
        if n.room and n.room_label and n.room_label != "Void":
            rooms.setdefault(n.room, n.room_label)
    reps = {rid: _rep_node(g, rid) for rid in rooms}
    entry = next((reps[r] for r, lab in rooms.items() if any(w in lab.lower() for w in ENTRY_WORDS)), None)
    if entry is None:
        floors = [f["id"] for f in g.floors]
        ground = [n for n in g.nodes.values() if not floors or n.floor == floors[0]]
        entry = min(ground or list(g.nodes.values()), key=lambda n: (n.label.isdigit() and int(n.label), n.label))
    return entry, {rid: (rooms[rid], reps[rid]) for rid in rooms if reps[rid]}


def trips(g, profile):
    entry, places = key_places(g)
    out = {}
    for rid, (label, node) in places.items():
        if node.id == entry.id:
            continue
        try:
            out[rid] = (label, plan(g, entry.id, node.id, profile))
        except NoRoute as ex:
            out[rid] = (label, str(ex))
    return entry, out


@tool(parse_docstring=True)
def building_overview(sid: str) -> str:
    """Floors, rooms, panos, walk-graph warnings, address and location of a building.

    Args:
        sid: Matterport model id.
    """
    m = _model(sid)
    g = graph(sid, annotated=False)
    rows = _index_row(sid) or {}
    floors = []
    for f in g.floors:
        ns = [n for n in g.nodes.values() if n.floor == f["id"]]
        labels = {}
        for n in ns:
            if n.room_label and n.room_label != "Void":
                labels[n.room_label] = labels.get(n.room_label, 0) + 1
        floors.append({"id": f["id"], "label": f["label"], "panos": len(ns), "walls": len(f["walls"]),
                       "rooms": labels})
    tags = {}
    for e in g.edges():
        for t in e.tags:
            tags[t] = tags.get(t, 0) + 1
    ann, err = read_annotations(sid)
    return _j({"sid": sid, "name": m.get("name"), "address": (m.get("address") or {}).get("address"),
               "geolocation": m.get("geolocation"), "segment": rows.get("segment"),
               "costar_property_id": rows.get("costar_property_id") or None,
               "floors": floors, "edge_tags": tags, "warnings": g.warnings,
               "annotations": "none yet" if ann is None and not err else err or
               f"{len(ann.get('hazards') or [])} hazards, {len(ann.get('nodes') or {})} annotated panos"})


@tool(parse_docstring=True)
def list_panos(sid: str, floor: str = "", room: str = "") -> str:
    """Scan positions with their label, floor, room, position (metres) and walkable neighbours.

    Args:
        sid: Matterport model id.
        floor: Optional floor id filter.
        room: Optional room label filter (case-insensitive substring).
    """
    check_floor(sid, floor)
    g = graph(sid, annotated=False)
    out = []
    for n in g.nodes.values():
        if floor and n.floor != floor:
            continue
        if room and room.lower() not in (n.room_label or "").lower():
            continue
        out.append({"pano_id": n.id, "label": n.label, "floor": n.floor, "room": n.room_label,
                    "x": round(n.x, 2), "y": round(n.y, 2),
                    "neighbours": [{"pano_id": v, "bearing_deg": round(math.degrees(math.atan2(
                        g.nodes[v].y - n.y, g.nodes[v].x - n.x))), "m": round(e.length, 1)}
                        for v, e in g.adj[n.id].items() if "vertical" not in e.tags]})
    return _j({"count": len(out), "panos": out[:200], "truncated": len(out) > 200})


@tool(parse_docstring=True, response_format="content")
def look(sid: str, pano_id: str, yaw_deg: float, pitch_deg: float = 0.0, fov_deg: float = 90.0) -> list:
    """Look out from a scan position. Returns the view as an image plus where it was taken.

    Args:
        sid: Matterport model id.
        pano_id: Scan position id (from list_panos or a candidate's nearest_pano).
        yaw_deg: Direction in the model frame, degrees. 0 is +x, counter-clockwise positive.
        pitch_deg: Up is positive, down negative. Use -60 to see the floor material.
        fov_deg: Field of view; 60 to zoom in, 100 for context.
    """
    from .. import perceive
    jpeg, meta = perceive.look(sid, pano_id, yaw_deg, pitch_deg, fov_deg)
    return _img_blocks(jpeg, meta)


def _focus(sid, items, panos, radius=3.0):
    """keep candidates near the listed panos (a route-scoped survey). empty panos = keep all."""
    ids = [x.strip() for x in (panos or "").split(",") if x.strip()]
    if not ids:
        return items
    g = graph(sid, annotated=False)
    pts = [g.nodes[i].xy for i in ids if i in g.nodes]
    keep = []
    for c in items:
        xy = (c["x"], c["y"]) if "x" in c else (((c["a"][0] + c["b"][0]) / 2, (c["a"][1] + c["b"][1]) / 2)
                                                 if "a" in c else None)
        if c.get("nearest_pano") in ids or (xy and any(math.hypot(xy[0] - p[0], xy[1] - p[1]) <= radius for p in pts)):
            keep.append(c)
    return keep


@tool(parse_docstring=True)
def overhang_candidates(sid: str, floor: str, panos: str = "") -> str:
    """Point-cloud candidates for protruding objects 27-80 in above the floor that a cane would miss.
    Each has nearest_pano and bearing_deg so you can confirm it with look().

    Args:
        sid: Matterport model id.
        floor: Floor id.
        panos: Optional comma-separated pano ids; only candidates within 3 m of them (route-scoped survey).
    """
    from .. import perceive
    check_floor(sid, floor)
    return _j(_focus(sid, perceive.overhang_candidates(sid, floor), panos))


@tool(parse_docstring=True)
def glass_suspects(sid: str, floor: str, panos: str = "") -> str:
    """Wall segments with unusually little point-cloud support: likely glass. Confirm with look().

    Args:
        sid: Matterport model id.
        floor: Floor id.
        panos: Optional comma-separated pano ids; only suspects within 3 m of them (route-scoped survey).
    """
    from .. import perceive
    check_floor(sid, floor)
    return _j(_focus(sid, perceive.glass_suspects(sid, floor), panos))


@tool(parse_docstring=True, response_format="content")
def aerial_view(sid: str, half_m: float = 60.0) -> list:
    """Vexcel aerial orthophoto centred on the building, north up, with a scale bar.

    Args:
        sid: Matterport model id.
        half_m: Metres from the centre to each edge.
    """
    from .. import perceive
    jpeg, meta = perceive.aerial(sid, half_m)
    return _img_blocks(jpeg, meta)


def _kit_files():
    return sorted(KIT.rglob("*.json*")) if KIT.exists() else []


@tool(parse_docstring=True)
def costar_record(sid: str) -> str:
    """CoStar building record for this model, joined by CoStar property id or Homes listing id.

    Args:
        sid: Matterport model id.
    """
    if sid == "synthetic":
        return _j({"synthetic": True, "year_built": 1984, "floors": 2, "rentable_sf": 4200,
                   "tenants": [{"name": "Acme Dental", "suite": "Office"}], "parking": "surface lot, 2 accessible stalls"})
    from .. import costar
    r = costar.lookup(sid)
    if r.get("error"):
        return _j({"error": r["error"]})
    rec = r["record"]
    s = json.dumps(rec, default=str)
    return _j({"key": r["key"], "kind": r["kind"], "file": r["file"], **costar.accessibility_facts(rec),
               "record": rec if len(s) < 12000 else s[:12000]})


def _flat_values(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _flat_values(v)
    elif isinstance(o, list):
        for v in o:
            yield from _flat_values(v)
    else:
        yield o


def audit(sid, profile="blind"):
    g = graph(sid)
    entry, ts = trips(g, profile)
    unreachable, uncertain, hazards, need_landmarks = [], set(), [], {}
    for rid, (label, r) in ts.items():
        if isinstance(r, str):
            unreachable.append({"room": label, "why": r})
            continue
        for a, b in zip(r.nodes, r.nodes[1:]):
            e = g.edge(a, b)
            if e.tags & {"uncertain", "vertical_unknown"}:
                uncertain.add((g.nodes[a].label, g.nodes[b].label, ",".join(sorted(e.tags))))
            for h in e.hazards:
                hazards.append({"room_trip": label, "kind": h.get("kind"), "note": h.get("note")})
        for m in maneuvers(g, r):
            if m["type"] in ("turn", "bear", "uturn", "arrive", "walk") and not g.nodes[m["node"]].landmarks:
                n = g.nodes[m["node"]]
                need_landmarks[n.id] = {"pano_id": n.id, "label": n.label, "floor": n.floor, "room": n.room_label,
                                        "maneuver": m["type"], "side": m.get("side")}
    return {"profile": profile, "start": {"pano_id": entry.id, "label": entry.label, "room": entry.room_label},
            "trips": len(ts), "reachable": len(ts) - len(unreachable), "unreachable": unreachable,
            "uncertain_links_used": [list(u) for u in sorted(uncertain)], "hazards_on_routes": hazards,
            "turn_points_without_landmarks": list(need_landmarks.values()), "graph_warnings": g.warnings}


@tool(parse_docstring=True)
def audit_routes(sid: str, profile: str = "blind") -> str:
    """Route from the entrance to every labelled room with the current /dossier/annotations.json applied.
    Reports unreachable rooms, uncertain links, hazards on routes and turn points with no landmark.

    Args:
        sid: Matterport model id.
        profile: blind, wheelchair or default.
    """
    rep = audit(sid, profile)
    rep["turn_points_without_landmarks"] = rep["turn_points_without_landmarks"][:60]
    return _j(rep)


def validate(sid):
    ann, err = read_annotations(sid)
    if err:
        return {"ok": False, "errors": [err]}
    if ann is None:
        return {"ok": False, "errors": ["no /dossier/annotations.json yet"]}
    g = graph(sid, annotated=False)
    floors = {f["id"] for f in g.floors}
    errors, warns = [], []
    for k in ann:
        if k not in ("nodes", "edges", "hazards", "meta"):
            warns.append(f"unknown top-level key {k!r} is ignored")
    for nid, info in (ann.get("nodes") or {}).items():
        if nid not in g.nodes:
            errors.append(f"nodes: {nid} is not a pano id")
            continue
        for lm in info.get("landmarks") or []:
            if not lm.get("text"):
                errors.append(f"nodes.{nid}: landmark without text")
            if lm.get("sense") not in SENSES:
                errors.append(f"nodes.{nid}: sense {lm.get('sense')!r} not in {sorted(SENSES)}")
    for key in ann.get("edges") or {}:
        a, _, b = key.partition("|")
        if a not in g.adj or b not in g.adj[a]:
            errors.append(f"edges: {key} is not a walk-graph edge")
    for i, h in enumerate(ann.get("hazards") or []):
        for f in ("x", "y", "floor", "kind", "note"):
            if f not in h:
                errors.append(f"hazards[{i}]: missing {f}")
        if h.get("floor") not in floors:
            errors.append(f"hazards[{i}]: floor {h.get('floor')!r} not in {sorted(floors)}")
        if h.get("kind") not in KINDS:
            errors.append(f"hazards[{i}]: kind {h.get('kind')!r} not in {sorted(KINDS)}")
        if not isinstance(h.get("radius", 0.6), (int, float)) or not 0 < h.get("radius", 0.6) <= 3:
            errors.append(f"hazards[{i}]: radius must be metres in (0, 3]")
        if not (h.get("evidence") or {}).get("pano_id"):
            warns.append(f"hazards[{i}]: no evidence.pano_id; hazards should be confirmed with look()")
    if errors:
        return {"ok": False, "errors": errors[:40], "warnings": warns[:20]}
    # what the annotations changed for routing
    base, ann_g = g, graph(sid)
    changed = []
    for profile in ("blind", "wheelchair"):
        _, before = trips(base, profile)
        _, after = trips(ann_g, profile)
        for rid, (label, r0) in before.items():
            r1 = after.get(rid, (label, None))[1]
            n0 = r0.nodes if not isinstance(r0, str) else None
            n1 = r1.nodes if r1 is not None and not isinstance(r1, str) else None
            if n0 != n1:
                changed.append({"profile": profile, "room": label,
                                "before_m": None if n0 is None else round(r0.length, 1),
                                "after_m": None if n1 is None else round(r1.length, 1)})
    hz_edges = sum(1 for e in ann_g.edges() if "hazard" in e.tags)
    return {"ok": True, "warnings": warns[:20], "hazards": len(ann.get("hazards") or []),
            "landmarked_panos": sum(1 for v in (ann.get("nodes") or {}).values() if v.get("landmarks")),
            "edges_marked_hazard": hz_edges, "routes_changed": changed[:40]}


@tool(parse_docstring=True)
def validate_annotations(sid: str) -> str:
    """Check /dossier/annotations.json against the schema and the walk graph, and report which routes it changed.

    Args:
        sid: Matterport model id.
    """
    return _j(validate(sid))


def focus_panos(g, route, radius=3.0):
    """panos within radius metres of a route's walking line: what a first visitor needs surveyed first."""
    from ..instruct import path as walk_path
    from ..geom import project
    pts = walk_path(g, route)
    segs = [(p, q) for p, q in zip(pts, pts[1:]) if not q[5] and p[2] == q[2]]
    out = []
    for n in g.nodes.values():
        on_route = n.id in route.nodes
        near = any(p[2] == n.floor and project(n.xy, p[:2], q[:2])[2] <= radius for p, q in segs)
        if on_route or near:
            out.append(n.id)
    return out


def route_focus(sid, goal, profile="blind"):
    """the route a first visitor will walk (entrance to goal) and the panos to survey first."""
    g = graph(sid, annotated=False)
    entry, _ = key_places(g)
    r = plan(g, entry.id, goal, profile)
    return {"start": entry.id, "goal": goal, "route": r.nodes, "length_m": round(r.length, 1),
            "focus": focus_panos(g, r)}


@tool(parse_docstring=True)
def building_models(sid: str) -> str:
    """Other Matterport scans of the same building (same CoStar property id, else same address).
    More than one means the building is several maps that must be joined through portals.

    Args:
        sid: Matterport model id.
    """
    from .. import building
    sibs = building.siblings(sid)
    confirmed = []
    for x in sibs:
        confirmed += building.read_portals(x["sid"])
    return _j({"sid": sid, "models": sibs, "count": len(sibs), "confirmed_portals": len(confirmed)})


@tool(parse_docstring=True)
def portal_candidates(sid: str) -> str:
    """Candidate links between this building's scans: elevators, stairs and entrances that may be the same
    place seen from two scans. Each has a, b ("sid:pano"), kind, direction, score and why.
    Confirm each with look() at both ends before writing it to /dossier/portals.json.

    Args:
        sid: Matterport model id (any scan of the building).
    """
    from .. import building
    sids = [x["sid"] for x in building.siblings(sid)]
    if len(sids) < 2:
        return _j({"sid": sid, "candidates": [], "note": "only one scan of this building; no portals needed"})
    graphs = {s: graph(s, annotated=False) for s in sids}
    return _j({"sid": sid, "models": sids, "candidates": building.candidates_portals(sids, graphs)})


def validate_portal_file(sid):
    from .. import building
    p = dossier_dir(sid) / "portals.json"
    if not p.exists():
        return {"ok": False, "errors": ["no /dossier/portals.json yet"]}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        return {"ok": False, "errors": [f"portals.json is not valid json: {ex}"]}
    sids = {x["sid"] for x in building.siblings(sid)}
    errors = []
    for i, x in enumerate(data.get("portals") or []):
        for k in ("a", "b", "kind"):
            if k not in x:
                errors.append(f"portals[{i}]: missing {k}")
        if x.get("kind") not in building.KINDS:
            errors.append(f"portals[{i}]: kind {x.get('kind')!r} not in {sorted(building.KINDS)}")
        for end in ("a", "b"):
            ref = str(x.get(end, ""))
            s_, _, pano = ref.partition(":")
            if s_ not in sids:
                errors.append(f"portals[{i}].{end}: {s_!r} is not a scan of this building")
            elif pano not in graph(s_, annotated=False).nodes:
                errors.append(f"portals[{i}].{end}: {pano!r} is not a pano in {s_}")
        if x.get("confirmed", True) and not (x.get("evidence") or {}):
            errors.append(f"portals[{i}]: confirmed without evidence; look() at both ends first")
    if errors:
        return {"ok": False, "errors": errors[:40]}
    g = building.building_graph(sid)
    reached = sorted({n.split(":")[0] for n in g.nodes})
    rooms = sorted({f"{n.room_label} ({n.id.split(':')[0]})" for n in g.nodes.values()
                    if n.room_label and n.room_label != "Void"})
    entry = next((n for n in g.nodes.values() if n.id.startswith(sid + ":")), None)
    unreachable = []
    if entry:
        for label in rooms:
            room, s_ = label.rsplit(" (", 1)
            goal = {n.id for n in g.nodes.values() if n.room_label == room and n.id.startswith(s_[:-1] + ":")}
            try:
                plan(g, entry.id, goal, "blind")
            except NoRoute:
                unreachable.append(label)
    return {"ok": True, "portals": len(data.get("portals") or []), "scans_joined": reached,
            "unreachable_from_" + sid: unreachable}


@tool(parse_docstring=True)
def validate_portals(sid: str) -> str:
    """Check /dossier/portals.json: ids exist, kinds are valid, confirmations have evidence, and every room
    in the joined scans is reachable from this scan.

    Args:
        sid: Matterport model id.
    """
    return _j(validate_portal_file(sid))


@tool(parse_docstring=True)
def place_seed(sid: str, floor: str = "") -> str:
    """The draft place catalog from the scan: one place per room (polygon, panos, entry panos, a guess at what it
    serves), floors, CoStar tenants, and entrances when the site is loaded. Unnamed or low-confidence rooms are the
    ones to look at and name. Write your part of the catalog to /dossier/places/<floor_id>.json using these ids.

    Args:
        sid: Matterport model id.
        floor: Optional floor id filter.
    """
    from .. import places
    check_floor(sid, floor)
    g = graph(sid)
    cat = places.seed_from_graph(g, sid, costar=places._costar(sid))
    out = []
    for p in cat["places"]:
        if floor and p.get("floor") != floor:
            continue
        rep = None
        if p["kind"] == "room" and p["panos"]:
            pts = [g.nodes[i] for i in p["panos"]]
            cx, cy = sum(n.x for n in pts) / len(pts), sum(n.y for n in pts) / len(pts)
            rep = min(pts, key=lambda n: math.hypot(n.x - cx, n.y - cy)).id
        out.append({k: p.get(k) for k in ("id", "name", "kind", "aliases", "serves", "floor", "entry_panos",
                                          "confidence", "unnamed", "matterport_label", "location_known")}
                   | {"panos": len(p.get("panos") or []), "central_pano": rep})
    return _j({"sid": sid, "places": out, "meta": cat["meta"]})


@tool(parse_docstring=True)
def validate_places(sid: str) -> str:
    """Check the place catalog (/dossier/places/*.json merged, plus /dossier/places.json if present): schema, ids that exist in the scan, feature anchors (pano + bearing), no authored
    polygons, the serves vocabulary, and names or aliases that point at two different places.

    Args:
        sid: Matterport model id.
    """
    from .. import places
    return _j(places.validate(sid))


LEAD_TOOLS = [building_overview, list_panos, audit_routes, validate_annotations, building_models,
              validate_portals, validate_places]
SCOUT_TOOLS = [building_overview, list_panos, look, overhang_candidates, glass_suspects]
LANDMARK_TOOLS = [building_overview, list_panos, look]
SITE_TOOLS = [building_overview, aerial_view, costar_record, list_panos, look]


def _outdoor_tools():
    from ..handoff import OUTDOOR_TOOLS  # entrance candidates, facade views, confirm (pick only)
    return list(OUTDOOR_TOOLS)


SITE_TOOLS = SITE_TOOLS + _outdoor_tools()
CARTOGRAPHER_TOOLS = [building_models, portal_candidates, list_panos, look, validate_portals]
PLACE_TOOLS = [place_seed, list_panos, look, costar_record, validate_places]
