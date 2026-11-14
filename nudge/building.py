"""buildings that are several matterport scans: a lobby scan, one per floor, suites.

each scan keeps its own coordinate frame. nothing is aligned metrically: a building graph is the per-scan
walk graphs side by side (ids "sid:pano", floors "sid:floor", each floor's frame = its sid) joined by
portals, which the router and navigator treat like floor changes. so every metre and degree the device
gets still comes from one scan's own geometry.

portals are proposed here deterministically (candidates_portals) and confirmed by the dossier agent's
building-cartographer, which writes /dossier/portals.json:
  {"portals": [{"a": "sidA:pano", "b": "sidB:pano", "kind": "elevator|stairs|door|entrance",
                "cost_m": 6.0, "direction": "up|down|level", "note": "...", "confirmed": true,
                "evidence": {...}}], "rejected": [...]}
"""
import csv
import json
import math
import re
from dataclasses import replace

from . import walkgraph
from .mp import ROOT
from .walkgraph import ELEVATOR, STAIRS, Edge, WalkGraph

DOSSIERS = ROOT / "cache" / "dossiers"
KINDS = {"elevator", "stairs", "door", "entrance"}
ENTRY = ("lobby", "entr", "foyer", "vestibule", "reception")
# synthetic scans share a fake costar id so building lookups work offline
SYNTHETIC = {"synthetic": {"name": "Floors 1-2", "costar_property_id": "SYN-1", "address": "1 Synthetic Way",
                           "floor_number": 1},
             "synthetic-lobby": {"name": "Lobby", "costar_property_id": "SYN-1", "address": "1 Synthetic Way",
                                 "floor_number": 0}}


def _index():
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def siblings(sid):
    """other scans of the same building: same costar property id, else same street address."""
    if sid in SYNTHETIC:
        me = SYNTHETIC[sid]
        return [{"sid": k, "name": v["name"], "address": v["address"], "match": "costar_property_id"}
                for k, v in SYNTHETIC.items() if v["costar_property_id"] == me["costar_property_id"]]
    rows = _index()
    me = next((r for r in rows if r["sid"] == sid), None)
    if not me:
        return [{"sid": sid, "name": None, "address": None, "match": "self"}]
    out, how = [], None
    if me.get("costar_property_id"):
        out = [r for r in rows if r.get("costar_property_id") == me["costar_property_id"]]
        how = "costar_property_id"
    if len(out) <= 1 and me.get("address") and me["address"] not in ("US", ""):
        key = me["address"].split(",")[0].strip().lower()
        out = [r for r in rows if r.get("address") and r["address"].split(",")[0].strip().lower() == key]
        how = "address"
    out = out or [me]
    return [{"sid": r["sid"], "name": r["name"], "address": r.get("address"), "match": how,
             "segment": r.get("segment"), "panos": r.get("pano_count"), "floors": r.get("floor_count")}
            for r in out]


def floor_number(name):
    """'35th Floor' -> 35, 'Level 2' -> 2, 'Lobby'/'Ground' -> 0, else None."""
    n = (name or "").lower()
    m = re.search(r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:floor|fl\b)", n) or re.search(r"(?:level|floor|lvl)\s*(\d+)", n)
    if m:
        return int(m.group(1))
    if any(w in n for w in ("lobby", "ground", "street", "entrance")):
        return 0
    return None


def _kind(label):
    l = (label or "").lower()
    if any(w in l for w in ELEVATOR):
        return "elevator"
    if any(w in l for w in STAIRS):
        return "stairs"
    if any(w in l for w in ENTRY):
        return "entrance"
    return None


def _reps(g):
    """one pano per (room, kind): the one nearest the room's middle."""
    rooms = {}
    for n in g.nodes.values():
        k = _kind(n.room_label)
        if k and n.room:
            rooms.setdefault((n.room, k), []).append(n)
    out = []
    for (rid, k), ns in rooms.items():
        cx, cy = sum(n.x for n in ns) / len(ns), sum(n.y for n in ns) / len(ns)
        out.append((k, min(ns, key=lambda n: math.hypot(n.x - cx, n.y - cy))))
    return out


def _model_name(sid):
    if sid in SYNTHETIC:
        return SYNTHETIC[sid]["name"]
    row = next((r for r in _index() if r["sid"] == sid), None)
    return row["name"] if row else sid


def candidates_portals(sids, graphs, names=None):
    """pairs of panos in different scans that could be the same elevator, stairs or entrance.
    graphs: {sid: WalkGraph} in each scan's own frame. the agent confirms these with look()."""
    names = names or {s: _model_name(s) for s in sids}
    fnum = {s: SYNTHETIC.get(s, {}).get("floor_number", floor_number(names.get(s))) for s in sids}
    reps = {s: _reps(graphs[s]) for s in sids}
    base = {("elevator", "elevator"): ("elevator", 0.8), ("stairs", "stairs"): ("stairs", 0.6),
            ("entrance", "entrance"): ("door", 0.35), ("entrance", "elevator"): None,
            ("elevator", "entrance"): None}
    out = []
    sids = list(sids)
    for i, sa in enumerate(sids):
        for sb in sids[i + 1:]:
            for ka, na in reps[sa]:
                for kb, nb in reps[sb]:
                    hit = base.get((ka, kb))
                    if not hit:
                        continue
                    kind, score = hit
                    fa, fb = fnum.get(sa), fnum.get(sb)
                    if fa is not None and fb is not None:
                        if fa == fb and kind in ("elevator", "stairs"):
                            score -= 0.3  # same floor number: an elevator between them is unlikely
                        if abs(fa - fb) == 1:
                            score += 0.1
                    direction = "level" if fa is None or fb is None or fa == fb else ("up" if fb > fa else "down")
                    why = [f"{ka} room '{na.room_label}' in {names.get(sa)}", f"{kb} room '{nb.room_label}' in {names.get(sb)}"]
                    if fa is not None and fb is not None:
                        why.append(f"floor numbers {fa} -> {fb}")
                    out.append({"id": f"pc{len(out)}", "a": f"{sa}:{na.id}", "b": f"{sb}:{nb.id}", "kind": kind,
                                "score": round(score, 2), "direction": direction,
                                "a_label": na.label, "b_label": nb.label, "a_room": na.room_label, "b_room": nb.room_label,
                                "a_map": names.get(sa), "b_map": names.get(sb), "why": "; ".join(why),
                                "cost_m": {"elevator": 8.0, "stairs": 6.0}.get(kind, 2.0)})
    out.sort(key=lambda c: -c["score"])
    for i, c in enumerate(out):
        c["id"] = f"pc{i}"
    return out[:24]


def read_portals(sid):
    p = DOSSIERS / sid / "portals.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [x for x in data.get("portals") or [] if x.get("confirmed", True)]


def _annotations(sid):
    p = DOSSIERS / sid / "annotations.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def scan_graph(sid, annotated=True):
    from .fetch import load
    return walkgraph.build(load(sid), _annotations(sid) if annotated else None)


def merge(graphs, portals, names=None):
    """one routable graph from several scans. graphs: {sid: WalkGraph}. portals: confirmed portal dicts."""
    names = names or {s: _model_name(s) for s in graphs}
    out = WalkGraph()
    for sid, g in graphs.items():
        single = len(g.floors) == 1
        for f in g.floors:
            label = names.get(sid) or sid
            out.floors.append({"id": f"{sid}:{f['id']}", "label": label if single else f"{label}, {f['label']}",
                               "walls": f["walls"], "frame": sid, "sid": sid, "scan_floor": f["id"]})
        for rid, r in g.rooms.items():
            out.rooms[f"{sid}:{rid}"] = {**r, "floor": f"{sid}:{r['floor']}"}
        for n in g.nodes.values():
            out.add_node(replace(n, id=f"{sid}:{n.id}", floor=f"{sid}:{n.floor}",
                                 room=f"{sid}:{n.room}" if n.room else None, tags=set(n.tags),
                                 landmarks=list(n.landmarks)))
        for e in g.edges():
            a, b = f"{sid}:{e.a}", f"{sid}:{e.b}"
            ne = Edge(a, b, e.length, set(e.tags), list(e.hazards), list(e.via), list(e.doors), e.min_clear,
                      dict(e.info))
            out.adj[a][b] = out.adj[b][a] = ne
        out.warnings += [f"{sid}: {w}" for w in g.warnings]
    for p in portals:
        a, b, kind = p["a"], p["b"], p.get("kind")
        if a not in out.nodes or b not in out.nodes or kind not in KINDS:
            out.warnings.append(f"portal {a} - {b} skipped (unknown pano or kind)")
            continue
        tags = ["portal", "vertical"] + ([kind] if kind in ("elevator", "stairs") else [])
        e = out.link(a, b, tags)
        e.length = float(p.get("cost_m") or 4.0)
        e.info = {"kind": kind, "note": p.get("note"), "direction": p.get("direction"),
                  "a_map": names.get(a.split(":")[0]), "b_map": names.get(b.split(":")[0])}
        # stored a->b; the reverse trip goes the other way
        e.info["reverse_direction"] = {"up": "down", "down": "up"}.get(p.get("direction"), p.get("direction"))
    return out


def building_graph(sid, annotated=True, extra_portals=()):
    """the scans of sid's building that confirmed portals reach, merged. a lone scan comes back namespaced too."""
    sibs = [s["sid"] for s in siblings(sid)]
    portals = []
    for s in sibs:
        portals += read_portals(s)
    portals += list(extra_portals)
    linked = {sid}
    changed = True
    while changed:  # only scans actually connected through confirmed portals
        changed = False
        for p in portals:
            sa, sb = p["a"].split(":")[0], p["b"].split(":")[0]
            if (sa in linked) != (sb in linked):
                linked |= {sa, sb}
                changed = True
    graphs = {s: scan_graph(s, annotated) for s in sorted(linked, key=lambda s: (s != sid, s))}
    seen, uniq = set(), []
    for p in portals:
        k = tuple(sorted((p["a"], p["b"])))
        if k not in seen and p["a"].split(":")[0] in graphs and p["b"].split(":")[0] in graphs:
            seen.add(k)
            uniq.append(p)
    return merge(graphs, uniq)
