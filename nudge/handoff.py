"""entrances: the portal between the vexcel outdoor graph and the matterport indoor graph.

candidates are deterministic (entry-labelled rooms, doors on the scan's outer boundary), mapped to the world
with georef and snapped to the outdoor graph. an agent may confirm which candidate is the accessible entrance
by looking at vexcel obliques, but it only ever picks among candidates; coordinates never come from a model.
"""
import json
import math
from dataclasses import dataclass, field

from langchain_core.tools import tool

from . import outdoor, vexcel
from .geom import project
from .geom import dist, point_in_poly
from .georef import OK_RMSE_M, outline
from .instruct import maneuvers, meters
from .navigator import Navigator
from .router import NoRoute, plan
from .walkgraph import NOT_DOORS, PASSABLE

OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}
SNAP_FAR_M = 25.0      # second try when nothing outdoor is within SNAP_M
SOURCE_ORDER = {"outer_door": 0, "entry_room": 1, "near_outline": 2}

ENTRY_WORDS = ("entr", "lobby", "vestibule", "foyer", "reception")
SNAP_M = 15.0
NEAR_WALL_M = 2.0


@dataclass
class Site:
    sid: str
    gi: object               # indoor WalkGraph (model frame)
    og: object               # outdoor WalkGraph (enu metres)
    geo: object              # georef.Georef
    portals: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def best_portal(self):
        if not self.portals:
            raise NoRoute("no entrance connects the street to this scan")
        confirmed = [p for p in self.portals if p["confirmed"] and p.get("accessible", True)]
        return (confirmed or self.portals)[0]


_SITES = {}


def register_site(site):
    _SITES[site.sid] = site
    return site


def _ground_floor(g):
    return g.floors[0]["id"] if g.floors else next(iter(g.nodes.values())).floor


def _ring_segments(ring):
    r = list(ring)
    return list(zip(r, r[1:] + r[:1]))


def _closest_on_ring(p, ring):
    best = None
    for a, b in _ring_segments(ring):
        t, c, d = project(p, a, b)
        if best is None or d < best[1]:
            best = (c, d, (a, b))
    return best


def _facade_faces(seg, inside):
    """compass direction the wall faces (its outward normal), from a ring segment and a point inside."""
    (ax, ay), (bx, by) = seg
    nx, ny = by - ay, -(bx - ax)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    if (inside[0] - mx) * nx + (inside[1] - my) * ny > 0:
        nx, ny = -nx, -ny
    deg = (90 - math.degrees(math.atan2(ny, nx))) % 360
    return ["north", "east", "south", "west"][int(((deg + 45) % 360) // 90)], round(deg)


def _nearest_pano(gi, floor, xy, max_m=8.0):
    """closest pano on the floor with a clear straight line to xy (a door point), or None."""
    for n in sorted((n for n in gi.nodes.values() if n.floor == floor), key=lambda n: dist(n.xy, xy)):
        if dist(n.xy, xy) > max_m:
            return None
        if not gi.blocked_line(xy, n.xy, floor):
            return n.id
    return None


def _is_entry(label):
    return any(w in (label or "").lower() for w in ENTRY_WORDS)


def entrance_candidates(gi, geo, og, sid, warnings=None):
    """portal records joining the outdoor graph to indoor panos, best first. real scans: the main floor
    may not be the lowest one, so every floor is searched."""
    for snap in (SNAP_M, SNAP_FAR_M):
        out = _entrance_candidates(gi, geo, og, sid, snap)
        if out:
            if snap > SNAP_M and warnings is not None:
                warnings.append(f"entrances snapped to the street from up to {snap:.0f} m; the approach path is rough")
            return out
    return []


def _entrance_candidates(gi, geo, og, sid, snap_m):
    cands = {}
    entry_polys = [(r["floor"], r["poly"]) for r in gi.rooms.values()
                   if _is_entry(r["label"]) and r["poly"] and len(r["poly"]) >= 3]

    def add(floor, ring, pano, door_xy, width, source):
        n = gi.nodes[pano]
        e, nn = geo.model_to_enu(*door_xy)
        fp_pt = _closest_on_ring((e, nn), og.footprint)[0] if getattr(og, "footprint", None) else (e, nn)
        out_id, d = outdoor.nearest_node(og, *fp_pt, max_m=snap_m)
        if not out_id:
            return
        seg = _closest_on_ring(door_xy, ring)[2]
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        faces, faces_deg = _facade_faces(seg, (cx, cy))
        # world-facing direction: rotate the model-frame facing by the georef rotation
        world_deg = (faces_deg - geo.theta_deg) % 360 if not geo.mirrored else (180 - geo.theta_deg - faces_deg) % 360
        rank = 0 if any(w in (n.room_label or "").lower() for w in ENTRY_WORDS[:1]) else \
            1 if _is_entry(n.room_label) else 2
        # a door that opens onto an entry-labelled room is the front door, even if no pano stands in that room
        onto_entry = any(f == floor and (point_in_poly(door_xy, poly) or _closest_on_ring(door_xy, poly)[1] < 0.3)
                         for f, poly in entry_polys)
        rec = {"outdoor_node": out_id, "sid": sid, "pano_id": pano, "kind": "entrance",
               "door_model": [round(door_xy[0], 3), round(door_xy[1], 3)],
               "width_m": round(width, 2) if width else None, "step": _step(og, fp_pt, out_id),
               "confirmed": False, "evidence": {}, "room": n.room_label, "source": source,
               "floor": floor, "onto_entry_room": onto_entry,
               "door_enu": [round(fp_pt[0], 2), round(fp_pt[1], 2)],
               "door_lonlat": list(og.enu.to_lonlat(*fp_pt)) if getattr(og, "enu", None) else None,
               "snap_m": round(d, 2), "faces": ["north", "east", "south", "west"][int(((world_deg + 45) % 360) // 90)],
               "osm_entrance": "entrance" in og.nodes[out_id].tags,
               "_rank": (0 if "entrance" in og.nodes[out_id].tags else 1, 0 if onto_entry else 1,
                         SOURCE_ORDER.get(source, 3), rank, d)}
        old = cands.get(out_id)
        if old is None or rec["_rank"] < old["_rank"]:
            cands[out_id] = rec

    floors = [f["id"] for f in gi.floors] or [_ground_floor(gi)]
    for floor in floors:
        ring = outline(gi, floor)
        if not ring or len(ring) < 3:
            continue
        # (i) doorways on the outer boundary: typed doors, gap doors, point-cloud doorways
        for a, b, kind in gi.walls(floor):
            if kind not in PASSABLE or kind in NOT_DOORS:
                continue
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if _closest_on_ring(mid, ring)[1] > 0.6:
                continue
            pano = _nearest_pano(gi, floor, mid)
            if pano:
                add(floor, ring, pano, mid, math.dist(a, b), "outer_door")
        # (ii) entry-labelled rooms: the door is where the room meets the outline
        for rid, r in gi.rooms.items():
            if r["floor"] != floor or not _is_entry(r["label"]):
                continue
            inside = [n for n in gi.nodes.values() if n.room == rid]
            for n in inside:
                c, d, _ = _closest_on_ring(n.xy, ring)
                if d <= 4.0:
                    add(floor, ring, n.id, c, None, "entry_room")
            poly = r["poly"]
            if not inside and poly and len(poly) >= 3:
                # no pano in the room: its door is the polygon point nearest the outline
                v = min(poly, key=lambda p: _closest_on_ring(p, ring)[1])
                c, d, _ = _closest_on_ring(v, ring)
                cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
                pano = _nearest_pano(gi, floor, (cx, cy), max_m=10.0)
                if d <= 1.5 and pano:
                    add(floor, ring, pano, c, None, "entry_room")
    if not cands:
        # (iii) last resort: panos close to the outline on the side facing the street
        for floor in floors:
            ring = outline(gi, floor)
            if not ring or len(ring) < 3:
                continue
            near = []
            for n in gi.nodes.values():
                if n.floor != floor:
                    continue
                c, d, _ = _closest_on_ring(n.xy, ring)
                if d <= 3.0:
                    e, nn = geo.model_to_enu(*c)
                    near.append((outdoor.nearest_node(og, e, nn)[1], n, c))
            for _, n, c in sorted(near, key=lambda t: t[0])[:3]:
                add(floor, ring, n.id, c, None, "near_outline")
    out = sorted(cands.values(), key=lambda r: r["_rank"])
    for r in out:
        r.pop("_rank")
    return out


# a near-rectangular footprint fits the scan equally well rotated 180°. front doors face the street:
# pick the rotation that puts the front door nearest a real road (osm class), not an alley.
ROAD_COST = {"trunk": 0, "primary": 0, "secondary": 0, "tertiary": 5, "residential": 10, "unclassified": 10,
             "living_street": 10}


def _street_dist(og, geo, xy):
    e, n = geo.model_to_enu(*xy)
    best = float("inf")
    for edge in og.edges():
        cost = ROAD_COST.get((edge.info or {}).get("highway"))
        if cost is None:
            continue
        pts = og.edge_points(edge.a, edge.b)
        d = min(project((e, n), a, b)[2] for a, b in zip(pts, pts[1:]))
        best = min(best, d + cost)
    return best


def _flip_geo(geo, alt, gi):
    """the georef rotated to the alternative angle about the scan's centre."""
    from .georef import Georef, sample_ring
    c = sample_ring(outline(gi)).mean(0)
    cx, cy = float(c[0]), float(c[1])
    ce = geo.model_to_enu(cx, cy)
    th = math.radians(alt["theta_deg"])
    my = -cy if geo.mirrored else cy
    tx = ce[0] - (math.cos(th) * cx - math.sin(th) * my)
    ty = ce[1] - (math.sin(th) * cx + math.cos(th) * my)
    return Georef(geo.enu, alt["theta_deg"], tx, ty, alt["rmse_m"], alt["inlier_frac"],
                  alt["rmse_m"] <= OK_RMSE_M, geo.mirrored, False,
                  [{"theta_deg": round(geo.theta_deg, 1), "rmse_m": round(geo.rmse_m, 3),
                    "inlier_frac": round(geo.inlier_frac, 3)}], source="footprint+street")


def resolve_flip(site):
    """swap to the 180° alternative when it clearly puts the front door on the street. returns True if swapped."""
    geo, gi, og = site.geo, site.gi, site.og
    alt = next((a for a in geo.alternatives if abs((a["theta_deg"] - geo.theta_deg) % 360 - 180) < 25), None)
    if not alt:
        return False
    front = next((p for p in site.portals if p.get("onto_entry_room")), site.portals[0] if site.portals else None)
    if front:
        ref = tuple(front["door_model"])
    else:
        polys = [r["poly"] for r in gi.rooms.values() if _is_entry(r["label"]) and r["poly"]]
        if not polys:
            return False
        ref = (sum(p[0] for p in polys[0]) / len(polys[0]), sum(p[1] for p in polys[0]) / len(polys[0]))
    flipped = _flip_geo(geo, alt, gi)
    d0, d1 = _street_dist(og, geo, ref), _street_dist(og, flipped, ref)
    if not d1 + 5 < d0:
        return False
    site.geo = flipped
    site.portals = entrance_candidates(gi, flipped, og, site.sid, site.warnings)
    site.warnings.append(f"the footprint fits either way round; turned the scan {alt['theta_deg'] - geo.theta_deg:+.0f}° "
                         f"so the front door faces the street ({d1:.0f} m vs {d0:.0f} m to the nearest main road)")
    return True


def _step(og, door_enu, out_id):
    """true/false/'unknown' for a step at the door, from dtm ground at the door vs the approach."""
    elev = getattr(og, "elevation", None)
    if not elev:
        return "unknown"
    try:
        zd = elev([og.enu.to_lonlat(*door_enu)])[0]
    except Exception:
        return "unknown"
    n = og.nodes[out_id]
    dz, run = abs(zd - n.z), max(0.5, math.dist(door_enu, n.xy))
    if dz < 0.03:
        return False
    if dz >= 0.12 and dz / run > 1 / 12:
        return True
    return "unknown"


def apply_confirmations(site, confirmations):
    for c in confirmations or []:
        for p in site.portals:
            if p["pano_id"] == c.get("pano_id"):
                p.update(confirmed=True, accessible=c.get("accessible", True),
                         evidence={**p.get("evidence", {}), **(c.get("evidence") or {})})
    site.portals.sort(key=lambda p: (not (p["confirmed"] and p.get("accessible", True)),))
    return site


def _entrance_name(p):
    room = (p.get("room") or "").lower()
    return "main entrance" if any(w in room for w in ("entr", "lobby", "foyer", "vestibule")) else "entrance"


def transition_text(p):
    parts = [f"You're at the {_entrance_name(p)}."]
    step = {False: "no step", True: "with a step"}.get(p.get("step"))
    if p.get("width_m"):
        parts.append(f"The door is {p['width_m']:.1f} meters wide" + (f", {step}." if step else "."))
    elif step:
        parts.append("No step at the door." if p.get("step") is False else "There is a step at the door.")
    if not p.get("confirmed"):
        parts.append("This entrance was not confirmed from imagery.")
    parts.append("Going inside.")
    return " ".join(parts)


def plan_door_to_door(start_latlon, sid, goal, profile="blind", site=None):
    """outdoor route to the chosen entrance + indoor route from its pano, as one maneuver list (metres)."""
    from .device import encode_step, route_packet
    site = site or load_site(sid)
    og, gi, geo = site.og, site.gi, site.geo
    portal = site.best_portal()
    lat, lon = start_latlon
    sx, sy = og.enu.to_enu(lon, lat)
    start, d0 = outdoor.nearest_node(og, sx, sy)
    warnings = list(site.warnings)
    if d0 > 30:
        warnings.append(f"start is {meters(d0)} from the nearest mapped path")
    oprof = outdoor.profile(profile)
    r_out = plan(og, start, portal["outdoor_node"], oprof)
    ms_out = maneuvers(og, r_out)
    last = ms_out[-1]
    last["transition"] = True
    walk = last["text"].split(", and you're there")[0].split(". You have arrived")[0].rstrip(".")
    if walk.startswith("Walk"):
        last["text"] = walk + f" to the {_entrance_name(portal)}."
    ms_out = outdoor.describe(og, r_out, ms_out)
    pk_out = route_packet(og, r_out, ms_out)
    text = transition_text(portal)
    r_in = plan(gi, portal["pano_id"], goal, profile)
    ms_in = maneuvers(gi, r_in)
    pk_in = route_packet(gi, r_in, ms_in)
    combined = [dict(m, leg="outdoor") for m in ms_out]
    combined.append({"type": "transition", "leg": "entrance", "text": text, "cue": "floor",
                     "pano_id": portal["pano_id"], "outdoor_node": portal["outdoor_node"], "distance": 0.0,
                     "along": r_out.length})
    combined += [dict(m, leg=sid, along=m.get("along", 0.0) + r_out.length) for m in ms_in]
    return {"portal": portal, "georef": geo.to_dict(), "warnings": warnings,
            "outdoor": {"route": r_out.to_dict(og), "length_m": round(r_out.length, 1), "maneuvers": ms_out,
                        "packets": [encode_step(p) for p in pk_out]},
            "transition": {"text": text, "pano_id": portal["pano_id"], "outdoor_node": portal["outdoor_node"]},
            "indoor": {"route": r_in.to_dict(gi), "length_m": round(r_in.length, 1), "maneuvers": ms_in,
                       "packets": [encode_step(p) for p in pk_in]},
            "maneuvers": combined, "length_m": round(r_out.length + r_in.length, 1)}


class DoorToDoorNavigator:
    """live guidance across the entrance: gps (lat/lon) outside, model (x, y, floor) inside."""

    def __init__(self, site, goal, profile="blind"):
        self.site, self.goal, self.profile = site, goal, profile
        self.portal = site.best_portal()
        self.outside = Navigator(site.og, self.portal["outdoor_node"], outdoor.profile(profile))
        self.inside = None
        self.phase = "outdoor"

    def _xy(self, lat, lon):
        return self.site.og.enu.to_enu(lon, lat)

    @staticmethod
    def _enu_heading(compass_deg):
        return None if compass_deg is None else math.radians(90.0 - compass_deg)

    def start(self, lat, lon, compass_deg=None):
        x, y = self._xy(lat, lon)
        return [dict(e, phase="outdoor") for e in self.outside.start(x, y, outdoor.FLOOR, self._enu_heading(compass_deg))]

    def update(self, lat=None, lon=None, x=None, y=None, floor=None, compass_deg=None, heading=None):
        if self.phase == "outdoor":
            ox, oy = self._xy(lat, lon)
            ev = [dict(e, phase="outdoor") for e in self.outside.update(ox, oy, outdoor.FLOOR, self._enu_heading(compass_deg))]
            if self.outside.done:
                ev = [e for e in ev if e["type"] != "arrive"]
                ev.append({"type": "transition", "phase": "entrance", "text": transition_text(self.portal), "cue": "floor"})
                p = self.site.gi.nodes[self.portal["pano_id"]]
                self.inside = Navigator(self.site.gi, self.goal, self.profile)
                h = self.site.geo.heading_model_from_compass(compass_deg) if compass_deg is not None else None
                ev += [dict(e, phase="indoor") for e in self.inside.start(p.x, p.y, p.floor, h)]
                self.phase = "indoor"
            return ev
        if x is None and lat is not None:  # gps indoors is rough, but better than nothing
            x, y = self.site.geo.lonlat_to_model(lon, lat)
            floor = floor or self.site.gi.nodes[self.portal["pano_id"]].floor
        if heading is None and compass_deg is not None:
            heading = self.site.geo.heading_model_from_compass(compass_deg)
        return [dict(e, phase="indoor") for e in self.inside.update(x, y, floor, heading)]

    @property
    def done(self):
        return self.phase == "indoor" and self.inside is not None and self.inside.done


# loading a real site (needs keys): indoor graph, vexcel footprint, georef, outdoor graph, candidates

def load_site(sid, refresh=False):
    if sid in _SITES and not refresh:
        return _SITES[sid]
    from .agents.tools import dossier_dir, graph
    from .fetch import load
    from .georef import georef_model
    m = load(sid)
    geo_ = m.get("geolocation") or {}
    lat, lon = float(geo_["lat"]), float(geo_["long"])
    early = []
    # real scans type every edge "wall": measure doorways in the point cloud once, then rebuild the graph
    from .walkgraph import DOORWAYS, compute_doorways
    if not sid.startswith("synthetic") and not (DOORWAYS / f"{sid}.json").exists():
        try:
            compute_doorways(sid, model=m)
            from .agents import tools as _t
            _t._graph_cached.cache_clear()
        except Exception as ex:  # no matterpak yet, etc.
            early.append(f"doorways were not measured from the point cloud ({ex}); exterior doors may be missed")
    gi = graph(sid)
    fp = vexcel.footprint(lat, lon)
    geo = georef_model(gi, lat, lon, fp)
    og = outdoor.build(lat, lon, footprint_lonlat=fp)
    site = Site(sid, gi, og, geo, warnings=early + list(og.warnings))
    if not geo.ok:
        site.warnings.append(f"scan-to-footprint fit is poor (rmse {geo.rmse_m:.1f} m, over {OK_RMSE_M} m); "
                             f"entrance positions may be off by a few meters")
    if geo.ambiguous:
        site.warnings.append("footprint is symmetric; scan rotation is ambiguous until an entrance is confirmed")
    site.portals = entrance_candidates(gi, geo, og, sid, site.warnings)
    resolve_flip(site)
    conf = dossier_dir(sid) / "entrances.json"
    if conf.exists():
        apply_confirmations(site, json.loads(conf.read_text(encoding="utf-8")).get("confirmed"))
    return register_site(site)


# tools for the site-surveyor subagent: pick among candidates, never invent coordinates

def _site_or_err(sid):
    try:
        return load_site(sid), None
    except Exception as ex:
        return None, f"site for {sid} unavailable: {ex}"


@tool(parse_docstring=True)
def entrance_candidates_tool(sid: str) -> str:
    """Candidate entrances where the street path meets the indoor scan, best first. Each has the indoor pano, the
    door position, width, step, which way the facade faces, and how far the sidewalk is.

    Args:
        sid: Matterport model id.
    """
    site, err = _site_or_err(sid)
    if err:
        return json.dumps({"error": err})
    return json.dumps({"georef": site.geo.to_dict(), "warnings": site.warnings,
                       "candidates": [dict(p, index=i) for i, p in enumerate(site.portals)]}, default=str)


@tool(parse_docstring=True, response_format="content")
def facade_view(sid: str, candidate: int, direction: str = "") -> list:
    """Vexcel oblique image of the facade at a candidate entrance, with a crosshair on the door point.

    Args:
        sid: Matterport model id.
        candidate: Index from entrance_candidates_tool.
        direction: camera heading: north, south, east or west. Empty looks at the facade head-on
            (the opposite of the side it faces).
    """
    import base64
    site, err = _site_or_err(sid)
    if err:
        return [{"type": "text", "text": err}]
    if not 0 <= candidate < len(site.portals):
        return [{"type": "text", "text": f"candidate must be 0..{len(site.portals) - 1}"}]
    p = site.portals[candidate]
    lon, lat = p["door_lonlat"]
    # vexcel's oblique-<dir> is shot looking <dir>, so a facade facing east is seen in the west-looking image
    d = direction or OPPOSITE[p["faces"]]
    jpeg = vexcel.oblique_image(lat, lon, d)
    return [{"type": "text", "text": json.dumps({"candidate": candidate, "pano_id": p["pano_id"], "direction": d,
                                                 "door_lonlat": p["door_lonlat"]})},
            {"type": "image", "base64": base64.b64encode(jpeg).decode(), "mime_type": "image/jpeg"}]


@tool(parse_docstring=True)
def confirm_entrance(sid: str, pano_id: str, accessible: bool = True, note: str = "") -> str:
    """Mark one candidate as the entrance you verified in the imagery. Only candidates are accepted.

    Args:
        sid: Matterport model id.
        pano_id: The candidate's pano_id.
        accessible: False if you saw steps with no ramp at this door.
        note: What you saw, e.g. "automatic sliding doors, level with the sidewalk".
    """
    from .agents.tools import dossier_dir
    site, err = _site_or_err(sid)
    if err:
        return json.dumps({"error": err})
    if pano_id not in {p["pano_id"] for p in site.portals}:
        return json.dumps({"error": "not a candidate; pick a pano_id from entrance_candidates_tool"})
    path = dossier_dir(sid) / "entrances.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"confirmed": []}
    data["confirmed"] = [c for c in data["confirmed"] if c["pano_id"] != pano_id]
    data["confirmed"].append({"pano_id": pano_id, "accessible": accessible, "evidence": {"note": note}})
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    apply_confirmations(site, data["confirmed"])
    return json.dumps({"ok": True, "best": site.best_portal()}, default=str)


OUTDOOR_TOOLS = [entrance_candidates_tool, facade_view, confirm_entrance]
