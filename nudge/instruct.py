"""route geometry -> maneuvers a person can follow without seeing."""
from .geom import dist, heading, project, turn_angle

DOOR_NARROW_M = 0.9  # matches walkgraph: narrower doorways get called out

SIMPLIFY_M = 0.6   # pano jitter smaller than this is not a turn
STRAIGHT_DEG = 25

# vibration patterns (ms on/off) for navigator.vibrate on the phone
CUES = {
    "straight": [60],
    "left": [120, 80, 120],
    "right": [120, 80, 120, 80, 120],
    "bear_left": [60, 60, 120],
    "bear_right": [60, 60, 120, 60, 120],
    "uturn": [400, 100, 400],
    "floor": [200, 100, 200, 100, 200],
    "arrive": [60, 60, 60, 60, 300],
    "hazard": [30, 30, 30, 30, 30, 30, 30, 30, 30],
    "off_route": [500],
    "unmapped": [600, 400, 600, 400, 600],  # slow, both sides: stop, this isn't mapped
}


def meters(m):
    """spoken distance: half-metre steps up close, whole metres beyond 10."""
    if m < 0.75:
        return "less than a meter"
    v = round(m * 2) / 2 if m < 10 else round(m)
    v = int(v) if v == int(v) else v
    return f"{v} meter" if v == 1 else f"{v} meters"


def _simplify(ids, pts, eps):
    """douglas-peucker that keeps node ids."""
    if len(pts) < 3:
        return list(range(len(pts)))
    a, b = pts[0], pts[-1]
    far, fi = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = project(pts[i], a, b)[2]
        if d > far:
            far, fi = d, i
    if far <= eps:
        return [0, len(pts) - 1]
    left = _simplify(ids[:fi + 1], pts[:fi + 1], eps)
    right = _simplify(ids[fi:], pts[fi:], eps)
    return left + [fi + i for i in right[1:]]


def classify(angle):
    a = abs(angle)
    side = "left" if angle > 0 else "right"
    if a < STRAIGHT_DEG:
        return "straight", None
    if a < 55:
        return "bear", side
    if a < 135:
        return "turn", side
    return "uturn", side


def _cue(kind, side):
    if kind == "turn":
        return side
    if kind == "bear":
        return f"bear_{side}"
    return kind


def _leg_notes(g, ids):
    """what happens along a leg: doorways, rooms entered, hazards."""
    notes, hazards, seen_rooms = [], [], set()
    for a, b in zip(ids, ids[1:]):
        e = g.edge(a, b)
        doors = g.edge_doors(a, b) if hasattr(g, "edge_doors") else []
        narrow = any(d["width"] < DOOR_NARROW_M for d in doors)
        if "door" in e.tags or doors:
            if narrow and "through a narrow doorway" not in notes:
                notes = [n for n in notes if n != "through a doorway"] + ["through a narrow doorway"]
            elif not narrow and not any(n.startswith("through a") for n in notes):
                notes.append("through a doorway")
        nb = g.nodes[b]
        if "room_change" in e.tags and nb.room_label and nb.room_label not in ("Void",) \
                and nb.room_label not in seen_rooms:
            seen_rooms.add(nb.room_label)
            notes.append(f"into the {nb.room_label.lower()}")
        for h in e.hazards:
            if h not in hazards:
                hazards.append(h)
    return notes, hazards


def _landmark(n):
    """prefer what you can feel or hear over what you can see."""
    order = {"underfoot": 0, "touch": 1, "sound": 2, "air": 3}
    lm = sorted(n.landmarks, key=lambda l: order.get(l.get("sense"), 9))
    return lm[0]["text"] if lm else None


def maneuvers(g, route):
    ids = route.nodes
    if len(ids) == 1:
        return [{"type": "arrive", "node": ids[0], "along": 0.0, "walk_along": 0.0, "distance": 0.0,
                 "turn_deg": None, "side": None, "text": "You are already there.", "cue": "arrive"}]

    # split into same-floor legs and floor changes
    legs, cur = [], [ids[0]]
    for a, b in zip(ids, ids[1:]):
        if "vertical" in g.edge(a, b).tags:
            if len(cur) == 1 and legs and legs[-1][0] == "floor" and legs[-1][1][-1] == a:
                legs[-1][1].append(b)  # still riding: one floor change, not two
            else:
                legs.append(("walk", cur))
                legs.append(("floor", [a, b]))
            cur = [b]
        else:
            cur.append(b)
    legs.append(("walk", cur))

    # along = route metres incl. vertical edges (what the navigator tracks);
    # walk_along = horizontal walking metres only (what the device counts down)
    along_at, walk_at, acc, wacc = {ids[0]: 0.0}, {ids[0]: 0.0}, 0.0, 0.0
    for a, b in zip(ids, ids[1:]):
        e = g.edge(a, b)
        acc += e.length
        wacc += 0.0 if "vertical" in e.tags else e.length
        along_at.setdefault(b, acc)
        walk_at.setdefault(b, wacc)

    out = []
    for kind, leg in legs:
        if kind == "floor":
            out.append(_floor_maneuver(g, leg, along_at, walk_at))
            continue
        if len(leg) < 2:
            continue
        pts = [g.nodes[x].xy for x in leg]
        keep = _simplify(leg, pts, SIMPLIFY_M)
        k_ids = [leg[i] for i in keep]
        k_pts = [pts[i] for i in keep]
        first = not out
        if first:
            out.append({"type": "depart", "node": k_ids[0], "along": 0.0, "walk_along": 0.0, "distance": 0.0,
                        "turn_deg": None, "heading": heading(k_pts[0], k_pts[1]), "text": "", "cue": "straight"})
        for i in range(1, len(k_ids)):
            seg_ids = leg[keep[i - 1]:keep[i] + 1]
            d = sum(g.edge(a, b).length for a, b in zip(seg_ids, seg_ids[1:]))
            notes, hazards = _leg_notes(g, seg_ids)
            last = i == len(k_ids) - 1
            node = g.nodes[k_ids[i]]
            ang = None
            if last:
                kind_, side = "arrive" if leg[-1] == ids[-1] else "floor_next", None
            else:
                ang = turn_angle(heading(k_pts[i - 1], k_pts[i]), heading(k_pts[i], k_pts[i + 1]))
                kind_, side = classify(ang)
            if kind_ == "floor_next":
                target = "elevator" if "elevator" in node.tags else "stairs" if "stairs" in node.tags else "connection"
                notes = [x for x in notes if target not in x]
            walk = f"Walk {meters(d)}"
            if notes:
                walk += ", " + ", ".join(notes)
            lm = _landmark(node)
            if kind_ == "arrive":
                where = node.room_label.lower() if node.room_label and node.room_label != "Void" else None
                text = f"{walk}, and you're there." if not where or f"into the {where}" in notes else \
                    f"{walk} to the {where}."
            elif kind_ == "floor_next":
                text = f"{walk} to the {target}."
            elif kind_ == "straight":
                text = f"{walk}, continue straight."
            elif kind_ == "uturn":
                text = f"{walk}, then turn around to your {side}."
            else:
                text = f"{walk}, then {kind_} {side}."
            if lm:
                text += f" Landmark: {lm}."
            warn = [h.get("note") or h.get("kind", "obstacle").replace("_", " ") for h in hazards]
            m = {"type": kind_ if kind_ != "floor_next" else "walk", "side": side, "node": k_ids[i],
                 "along": along_at[k_ids[i]], "walk_along": walk_at[k_ids[i]], "distance": d,
                 "turn_deg": None if ang is None else round(ang), "text": text,
                 "cue": "arrive" if kind_ == "arrive" else _cue(kind_, side) if side else "straight"}
            if warn:
                m["warnings"] = warn
                m["text"] = "Caution: " + "; ".join(warn) + ". " + m["text"]
            out.append(m)
    return out


def _floor_maneuver(g, leg, along_at, walk_at):
    """one floor change or map switch, possibly over several back-to-back vertical links."""
    a, b = g.nodes[leg[0]], g.nodes[leg[-1]]
    edges = [g.edge(x, y) for x, y in zip(leg, leg[1:])]
    tags = set().union(*(e.tags for e in edges))
    portal = None
    for (x, y), e in zip(zip(leg, leg[1:]), edges):
        if "portal" in e.tags:
            portal = dict(e.info)
            if x != e.a:  # walked b -> a: the other way
                portal["direction"] = e.info.get("reverse_direction")
            break
    how = "elevator" if "elevator" in tags else "stairs" if "stairs" in tags else \
        (portal or {}).get("kind") or "way up"
    fl = next((f["label"] for f in g.floors if f["id"] == b.floor), b.floor)
    # z is only comparable inside one scan; a portal says its own direction
    frame = getattr(g, "frame", lambda f: "")
    if frame(a.floor) == frame(b.floor):
        rise = b.z - a.z
    else:
        rise = {"up": 1.0, "down": -1.0}.get((portal or {}).get("direction"), 0.0)
    up = "up " if rise > 0 else "down " if rise < 0 else ""
    if how == "elevator":
        text = f"Take the elevator {up}to {fl}."
    elif how == "stairs":
        text = f"Take the stairs {up}to {fl}. Handrail recommended."
    elif how in ("door", "entrance"):
        text = f"Go through the {(portal or {}).get('note') or 'doors'} into {fl}."
    else:
        text = f"Go {up}to {fl}; the connection here is uncertain, ask for help if needed."
    if portal and portal.get("note") and how in ("elevator", "stairs"):
        text += f" {portal['note'][0].upper()}{portal['note'][1:].rstrip('.')}."
    # distance stays 0: no walking. the vertical links' metres go in vertical_m
    return {"type": "floor", "node": leg[0], "to_node": leg[-1], "along": along_at[leg[0]],
            "walk_along": walk_at[leg[0]], "distance": 0.0, "turn_deg": None,
            "vertical_m": sum(e.length for e in edges), "rise_m": rise, "to_floor": b.floor,
            "text": text, "cue": "floor", "via": how, "portal": bool(portal)}


def path(g, route):
    """the exact walking polyline of a route: [(x, y, floor, along, edge_index, vertical)].
    along matches maneuver 'along' (route metres incl. vertical links)."""
    ids, out, acc = route.nodes, [], 0.0
    if not ids:
        return out
    n0 = g.nodes[ids[0]]
    out.append((n0.x, n0.y, n0.floor, 0.0, -1, False))
    for i, (a, b) in enumerate(zip(ids, ids[1:])):
        e = g.edge(a, b)
        nb = g.nodes[b]
        if "vertical" in e.tags or g.nodes[a].floor != nb.floor:
            acc += e.length
            out.append((nb.x, nb.y, nb.floor, acc, i, True))
            continue
        pts = g.edge_points(a, b) if hasattr(g, "edge_points") else [g.nodes[a].xy, nb.xy]
        for p, q in zip(pts, pts[1:]):
            acc += dist(p, q)
            out.append((q[0], q[1], nb.floor, acc, i, False))
    return out


def door_points(g, route):
    """every doorway the route goes through: [{along, walk_along, x, y, floor, width, edge_index, into}]."""
    ids, out = route.nodes, []
    acc = wacc = 0.0
    for i, (a, b) in enumerate(zip(ids, ids[1:])):
        e = g.edge(a, b)
        if "vertical" in e.tags:
            acc += e.length
            continue
        pts = g.edge_points(a, b) if hasattr(g, "edge_points") else [g.nodes[a].xy, g.nodes[b].xy]
        doors = g.edge_doors(a, b) if hasattr(g, "edge_doors") else []
        nb = g.nodes[b]
        into = nb.room_label if nb.room_label and nb.room_label != "Void" else None
        run, found = 0.0, set()
        for p, q in zip(pts, pts[1:]):
            run += dist(p, q)
            for k, d in enumerate(doors):
                if k not in found and abs(q[0] - d["x"]) < 1e-6 and abs(q[1] - d["y"]) < 1e-6:
                    found.add(k)
                    out.append({"along": acc + run, "walk_along": wacc + run, "x": d["x"], "y": d["y"],
                                "floor": nb.floor, "width": d["width"], "edge_index": i, "into": into})
        # a doorway the waypoints couldn't square up still counts, at its projection
        for k, d in enumerate(doors):
            if k not in found:
                t = project((d["x"], d["y"]), g.nodes[a].xy, nb.xy)[0]
                out.append({"along": acc + t * e.length, "walk_along": wacc + t * e.length, "x": d["x"],
                            "y": d["y"], "floor": nb.floor, "width": d["width"], "edge_index": i, "into": into})
        acc += e.length
        wacc += e.length
    return sorted(out, key=lambda d: d["along"])


def orient(user_heading, route_heading):
    """first instruction: how to turn your body to face the route (headings in radians, model frame)."""
    ang = turn_angle(user_heading, route_heading)
    kind, side = classify(ang)
    if kind == "straight":
        return "You are facing the right way.", "straight"
    if kind == "uturn":
        return "Turn around.", "uturn"
    clock = round((-ang % 360) / 30) or 12
    return f"{'Bear' if kind == 'bear' else 'Turn'} {side}, toward {clock} o'clock.", _cue(kind, side)


def speak_all(ms):
    return [m["text"] for m in ms if m["text"]]
