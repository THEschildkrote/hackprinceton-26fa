"""live guidance: feed it positions, it tells you what to say and buzz. no model in the loop.

positions are in the model frame (metres). in the browser demo they come from the showcase sdk camera;
on a phone they would come from whatever localizer we plug in. for a multi-scan building (building.py)
pass sid= and the floor is namespaced "sid:floor".

what it tracks against is the exact walking polyline (instruct.path): through doorway middles, off walls.
when the position leaves the map it stops giving turns (it never invents geometry) and walks you back
along your own breadcrumb trail to the last place it knew.
"""
from .geom import dist, heading as heading_of, poly_len, project, turn_angle
from .instruct import CUES, _landmark, door_points, maneuvers, meters, orient, path
from .router import NoRoute, plan

SNAP_RADIUS = 6.0
LOOKAHEAD_M = 1.5    # point on the route we steer the user toward
START_SLACK_M = 2.0  # how much farther than the nearest pano a start pano may be
DOOR_CUE_M = 2.0     # "doorway ahead" this far before the door's middle
HAZARD_NEAR_M = 3.5
UNMAPPED_ROUTE_M = 3.0   # outside every room and this far from the route = off the map
TRAIL_MAX_M = 30.0       # past this, retracing your steps isn't a safe plan any more
TELEPORT_M = 8.0         # a jump bigger than this between fixes isn't a walk we can retrace
STOP_TEXT = "This area isn't in my map. Please stop and use your cane; I can call for help."


def snap(g, x, y, floor, skip=()):
    """nearest pano you can walk to in a straight line; falls back to nearest on the floor."""
    cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.floor == floor and n.id not in skip),
                   key=lambda t: t[0])
    if not cands:
        cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.id not in skip), key=lambda t: t[0])
    for d, n in cands:
        if d > SNAP_RADIUS:
            break
        if not g.blocked_line((x, y), n.xy, n.floor):
            return n.id, d
    return cands[0][1].id, cands[0][0]


def visible(g, x, y, floor, k=4, skip=()):
    """up to k nearest panos on this floor with a clear straight line from (x, y)."""
    cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.floor == floor and n.id not in skip),
                   key=lambda t: t[0])
    out = [(n.id, d) for d, n in cands if d <= SNAP_RADIUS and not g.blocked_line((x, y), n.xy, floor)]
    return out[:k]


def _ns(floor, sid):
    return f"{sid}:{floor}" if sid and not str(floor).startswith(f"{sid}:") else floor


class Navigator:
    def __init__(self, g, goal, profile="blind", announce_far=8.0, announce_near=2.0,
                 off_route_m=2.0, off_route_hits=2, arrive_m=1.2):
        self.g, self.goal, self.profile = g, goal, profile
        self.far, self.near = announce_far, announce_near
        self.off_m, self.off_hits, self.arrive_m = off_route_m, off_route_hits, arrive_m
        self.blocked_edges, self.blocked_nodes = set(), set()
        self.route = None
        self.done = False
        self.reroutes = 0
        self.pos = None       # last (x, y, floor) we were given
        self.heading = None   # last heading, radians, model frame
        self.room = None      # room id we last announced or started in
        self._room_cand, self._room_hits = None, 0
        self.unmapped = False
        self.last_on_map = None   # (x, y, floor)
        self.trail = None         # breadcrumbs since leaving the map, oldest first; None = can't retrace
        self.hazard_near = False
        self.floors = {f["id"] for f in g.floors}

    # route bookkeeping

    def _start_node(self, x, y, floor):
        """the nearby pano that makes the whole trip shortest, so a reroute never walks you backwards first.
        blocked panos are never a start, even the one you're standing on."""
        best = None
        near = visible(self.g, x, y, floor, skip=self.blocked_nodes)
        for nid, d in near:
            # the walk to the start pano gets no instructions, so keep it short
            if d > near[0][1] + START_SLACK_M:
                continue
            try:
                c = d + plan(self.g, nid, self.goal, self.profile, self.blocked_edges, self.blocked_nodes).cost
            except NoRoute:
                continue
            if best is None or c < best[0]:
                best = (c, nid)
        return best[1] if best else snap(self.g, x, y, floor, skip=self.blocked_nodes)[0]

    def _set_route(self, start_node):
        self.route = plan(self.g, start_node, self.goal, self.profile, self.blocked_edges, self.blocked_nodes)
        self.ms = maneuvers(self.g, self.route)
        self.cum = [0.0]
        for a, b in zip(self.route.nodes, self.route.nodes[1:]):
            self.cum.append(self.cum[-1] + self.g.edge(a, b).length)
        self.path = path(self.g, self.route)
        self.doors = door_points(self.g, self.route)
        self.progress = 0.0
        self.mi = 1 if len(self.ms) > 1 else 0   # next maneuver to reach (0 is depart)
        self.said = set()
        self.warned = set()
        self.door_said = set()
        self.miss = 0

    def _summary(self):
        turns = sum(1 for m in self.ms if m["type"] in ("turn", "bear", "uturn"))
        floors = sum(1 for m in self.ms if m["type"] == "floor")
        s = f"Route ready: {meters(self.route.length)}, {turns} turn{'s' * (turns != 1)}"
        if floors:
            s += f", {floors} floor change{'s' * (floors != 1)}"
        return s + "."

    def _locate(self, x, y, floor):
        """(along, cross, clear) of the best spot on the walking polyline for this position."""
        best = None
        P = self.path
        for j in range(1, len(P)):
            p, q = P[j - 1], P[j]
            if q[5]:  # floor change / map switch: you're "on" it when standing at either end
                cands = []
                if q[2] == floor and dist((x, y), q[:2]) < self.off_m:
                    cands.append((q[3], dist((x, y), q[:2]), q[:2]))
                if p[2] == floor and dist((x, y), p[:2]) < self.off_m:
                    cands.append((p[3], dist((x, y), p[:2]), p[:2]))
            elif q[2] != floor:
                continue
            else:
                t, c, d = project((x, y), p[:2], q[:2])
                cands = [(p[3] + t * (q[3] - p[3]), d, c)]
            for cand in cands:
                # stay near where we were, so loops in the route don't make us jump
                score = cand[1] + 0.05 * max(0.0, self.progress - cand[0]) + \
                    0.02 * max(0.0, cand[0] - self.progress - 10)
                if best is None or score < best[0]:
                    best = (score, cand)
        if best is None:
            return None
        along, cross, c = best[1]
        clear = cross < 0.05 or not self.g.blocked_line((x, y), c, floor)
        return along, cross, clear

    def _on_map(self, x, y, floor):
        """(on_map, why). never guess: unknown floor, no pano in reach, or outside every room far from the route."""
        if floor not in self.floors:
            return False, "floor"
        if not visible(self.g, x, y, floor, k=1):
            return False, "no_pano"
        has_rooms = any(r["floor"] == floor and r["poly"] for r in self.g.rooms.values())
        # standing on a scan position is on the map, even outside every room (porches, entry landings)
        near = visible(self.g, x, y, floor, k=1)
        if near and near[0][1] <= 1.0:
            return True, None
        if has_rooms and self.g.room_at(x, y, floor) is None:
            loc = self._locate(x, y, floor) if self.route else None
            if loc is None or loc[1] > UNMAPPED_ROUTE_M:
                return False, "outside_rooms"
        return True, None

    # public api

    def _seen(self, x, y, floor, heading):
        self.pos = (x, y, floor)
        if heading is not None:
            self.heading = heading

    def start(self, x, y, floor, heading=None, sid=None):
        floor = _ns(floor, sid)
        self._seen(x, y, floor, heading)
        on, _ = self._on_map(x, y, floor)
        if not on:
            self.unmapped, self.trail = True, None
            return [{"type": "unmapped", "text": STOP_TEXT, "cue": "unmapped", "can_return": False}]
        self.last_on_map = (x, y, floor)
        self.room = self._room_of(x, y, floor)
        node = self._start_node(x, y, floor)
        try:
            self._set_route(node)
        except NoRoute as ex:
            return [{"type": "no_route", "text": f"Sorry, I can't find a route. {ex}", "cue": "off_route"}]
        ev = [{"type": "route", "text": self._summary(), "cue": None}]
        dep = self.ms[0]
        if heading is not None and dep["type"] == "depart":
            t, cue = orient(heading, dep["heading"])
            ev.append({"type": "orient", "text": t, "cue": cue})
        ev += self._announce_leg()
        return ev

    def _announce_leg(self):
        if self.mi >= len(self.ms):
            return []
        m = self.ms[self.mi]
        self.said.add((self.mi, "leg"))
        # the leg text already says what's coming; don't repeat it seconds later
        if m["type"] == "floor" or m.get("distance", 0) <= self.far:
            self.said.add((self.mi, "soon"))
        if m["type"] in ("floor", "walk"):
            self.said.add((self.mi, "now"))
        return [{"type": "leg", "text": m["text"], "cue": "hazard" if m.get("warnings") else None, "maneuver": self.mi}]

    def _room_of(self, x, y, floor):
        rid = self.g.room_at(x, y, floor)
        if rid is None:
            nid, _ = snap(self.g, x, y, floor)
            rid = self.g.nodes[nid].room
        r = self.g.rooms.get(rid) if rid else None
        return rid if r and r["label"] not in (None, "Void") else None

    def _room_events(self, x, y, floor):
        rid = self._room_of(x, y, floor)
        if rid is None or rid == self.room:
            self._room_cand, self._room_hits = None, 0
            return []
        if self.room is None:
            self.room = rid
            return []
        # two fixes in a row, so jitter on a doorway doesn't flip-flop
        self._room_hits = self._room_hits + 1 if rid == self._room_cand else 1
        self._room_cand = rid
        if self._room_hits < 2:
            return []
        self.room, self._room_cand, self._room_hits = rid, None, 0
        label = self.g.rooms[rid]["label"]
        entry = next((self.g.nodes[n] for n in (self.route.nodes if self.route else []) if self.g.nodes[n].room == rid),
                     None)
        if entry is None:
            inside = [n for n in self.g.nodes.values() if n.room == rid]
            entry = min(inside, key=lambda n: dist(n.xy, (x, y))) if inside else None
        lm = _landmark(entry) if entry else None
        text = f"Entering the {label.lower()}." + (f" {lm[0].upper()}{lm[1:].rstrip('.')}." if lm else "")
        return [{"type": "room", "text": text, "room": label, "cue": None}]

    def update(self, x, y, floor, heading=None, sid=None):
        floor = _ns(floor, sid)
        prev = self.pos
        self._seen(x, y, floor, heading)
        if self.done or self.route is None:
            return []
        on, why = self._on_map(x, y, floor)
        if not on:
            return self._unmapped(x, y, floor, why, prev)
        if self.unmapped:
            self.unmapped, self.trail = False, None
            self.last_on_map = (x, y, floor)
            self.room = self._room_of(x, y, floor)
            return [{"type": "remapped", "text": "You're back on the map.", "cue": "straight"}] + \
                self._reroute(x, y, floor, heading, "")
        self.last_on_map = (x, y, floor)
        ev = self._room_events(x, y, floor)
        loc = self._locate(x, y, floor)
        if loc is None or loc[1] > self.off_m or not loc[2]:
            self.miss += 1
            if self.miss >= self.off_hits:
                return ev + self._reroute(x, y, floor, heading, "You are off the route.")
            return ev
        self.miss = 0
        along, _, _ = loc
        self.progress = max(self.progress, along) if along - self.progress < 15 else along

        # hazards just ahead on the route
        self.hazard_near = False
        for i, (a, b) in enumerate(zip(self.route.nodes, self.route.nodes[1:])):
            e = self.g.edge(a, b)
            for h in e.hazards:
                if self.cum[i + 1] < self.progress or h.get("floor", floor) != floor:
                    continue
                if dist((x, y), (h["x"], h["y"])) < HAZARD_NEAR_M:
                    self.hazard_near = True
                    key = (h.get("x"), h.get("y"))
                    if key not in self.warned:
                        self.warned.add(key)
                        ev.append({"type": "hazard", "cue": "hazard",
                                   "text": f"Caution: {h.get('note') or h.get('kind', 'obstacle').replace('_', ' ')} ahead."})

        # doorways: square up before you reach them
        for k, d in enumerate(self.doors):
            if k in self.door_said or d["floor"] != floor:
                continue
            to_door = d["along"] - self.progress
            if to_door <= 0.2:
                self.door_said.add(k)
            elif to_door <= DOOR_CUE_M:
                self.door_said.add(k)
                kind = "Narrow doorway" if d["width"] < 0.9 else "Doorway"
                ev.append({"type": "door", "text": f"{kind} ahead, {meters(to_door)}.", "cue": None,
                           "width": d["width"]})

        while self.mi < len(self.ms):
            m = self.ms[self.mi]
            to_go = m["along"] - self.progress
            if m["type"] == "arrive":
                if to_go <= self.arrive_m:
                    self.done = True
                    where = self.g.nodes[m["node"]].room_label
                    ev.append({"type": "arrive", "cue": "arrive",
                               "text": f"You have arrived{' at the ' + where.lower() if where and where != 'Void' else ''}."})
                    return ev
            elif m["type"] == "floor":
                if floor == self.g.nodes[m["to_node"]].floor:
                    self.progress = max(self.progress, self.cum[self.route.nodes.index(m["to_node"])])
                    self.mi += 1
                    ev += self._announce_leg()
                    continue
                if to_go <= self.near and (self.mi, "now") not in self.said:
                    self.said.add((self.mi, "now"))
                    ev.append({"type": "now", "text": m["text"], "cue": "floor"})
                break
            else:
                if to_go <= 0.3:
                    self.mi += 1
                    ev += self._announce_leg()
                    continue
                verb = {"turn": f"turn {m['side']}", "bear": f"bear {m['side']}", "uturn": "turn around",
                        "straight": "continue straight", "walk": "continue"}.get(m["type"], "continue")
                if to_go <= self.near and (self.mi, "now") not in self.said:
                    self.said.add((self.mi, "now"))
                    ev.append({"type": "now", "text": f"{verb.capitalize()} now.", "cue": m["cue"]})
                elif to_go <= self.far and (self.mi, "soon") not in self.said and m["type"] != "straight":
                    self.said.add((self.mi, "soon"))
                    ev.append({"type": "soon", "text": f"In {meters(to_go)}, {verb}.", "cue": None})
            break
        return ev

    # off the map

    def _unmapped(self, x, y, floor, why, prev):
        here = (x, y, floor)
        if not self.unmapped:
            self.unmapped = True
            self.hazard_near = False
            lm = self.last_on_map
            ok = lm is not None and lm[2] == floor and dist(lm[:2], (x, y)) <= TELEPORT_M and \
                (prev is None or prev[2] != floor or dist(prev[:2], (x, y)) <= TELEPORT_M)
            self.trail = [lm, here] if ok else None
            return [self._unmapped_event(why)]
        if self.trail is None:
            return []
        last = self.trail[-1]
        if floor != last[2] or dist(last[:2], (x, y)) > TELEPORT_M:
            self.trail = None
            return [{"type": "unmapped", "text": STOP_TEXT, "cue": "unmapped", "can_return": False, "why": why}]
        if dist(last[:2], (x, y)) >= 0.3:
            self.trail.append(here)
        if poly_len([p[:2] for p in self.trail]) > TRAIL_MAX_M:
            self.trail = None
            return [{"type": "unmapped", "text": STOP_TEXT, "cue": "unmapped", "can_return": False, "why": why}]
        return []

    def _unmapped_event(self, why):
        if self.trail is None:
            return {"type": "unmapped", "text": STOP_TEXT, "cue": "unmapped", "can_return": False, "why": why}
        back = self.way_back()
        return {"type": "unmapped", "cue": "unmapped", "can_return": True, "why": why,
                "back_m": round(back["m"], 1),
                "text": f"This area isn't in my map. Turn around and walk back about {meters(back['m'])} the way you "
                        f"came; I'll tell you when you're back on the map."}

    def way_back(self):
        """retrace your own steps (positions we actually saw you at) to the last on-map point."""
        if not self.trail:
            return None
        pts = [p[:2] for p in reversed(self.trail)]
        return {"m": poly_len(pts), "points": pts}

    def block(self, x, y, floor, a, b=None, heading=None, why="That way is blocked.", sid=None):
        """exception hook: a door is locked or an elevator is out. block a node, or the edge a-b, and replan."""
        floor = _ns(floor, sid)
        if b is None:
            self.blocked_nodes.add(a)
        else:
            self.blocked_edges.add((a, b))
        return self._reroute(x, y, floor, heading, why)

    def _reroute(self, x, y, floor, heading, why):
        node = self._start_node(x, y, floor)
        self.reroutes += 1
        try:
            self._set_route(node)
        except NoRoute as ex:
            return [{"type": "no_route", "text": f"{why} I can't find another way. {ex}".strip(), "cue": "off_route"}]
        ev = [{"type": "reroute", "text": f"{why} New route, {meters(self.route.length)}.".strip(), "cue": "off_route"}]
        if heading is not None and self.ms[0]["type"] == "depart":
            t, cue = orient(heading, self.ms[0]["heading"])
            ev.append({"type": "orient", "text": t, "cue": cue})
        return ev + self._announce_leg()

    def _point_at(self, along):
        """(x, y) on the walking polyline at `along` metres, not past the next floor change."""
        P = self.path
        for j in range(1, len(P)):
            p, q = P[j - 1], P[j]
            if q[5]:
                if along < q[3]:
                    return p[:2]  # don't steer through the elevator; stop at its door
                continue
            if q[3] >= along:
                L = q[3] - p[3]
                t = 0.0 if L == 0 else (along - p[3]) / L
                return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
        return P[-1][:2]

    def live(self, heading=None):
        """numbers for a haptic device. metres to the next maneuver, and the signed angle (deg, + = left)
        from the user's heading to the way they should walk right now. off the map: metres and angle back
        along the breadcrumb trail, or nothing at all if we can't retrace."""
        base = {"distance_to_next_m": None, "bearing_to_next_deg": None, "next": None, "off": False,
                "done": self.done, "unmapped": self.unmapped, "hazard": False}
        h = self.heading if heading is None else heading
        if self.unmapped:
            back = self.way_back()
            if back and self.pos:
                base["distance_to_next_m"] = round(back["m"], 1)
                tgt = next((p for p in back["points"][1:] if dist(p, self.pos[:2]) >= 0.5), back["points"][-1])
                if h is not None and dist(tgt, self.pos[:2]) > 1e-6:
                    base["bearing_to_next_deg"] = round(turn_angle(h, heading_of(self.pos[:2], tgt)))
            return base
        if not self.route:
            return base
        nxt = self.ms[self.mi] if self.mi < len(self.ms) else None
        to_next = None if nxt is None else round(max(0.0, nxt["along"] - self.progress), 1)
        bearing = None
        if h is not None and self.pos is not None and not self.done:
            here = self.pos[:2]
            target = self._point_at(self.progress + LOOKAHEAD_M)
            if dist(here, target) < 0.3:
                # basically on the target: steer along the route instead
                target2 = self._point_at(self.progress + LOOKAHEAD_M + 1.0)
                here, target = target, target2
            if dist(here, target) >= 1e-6:
                bearing = round(turn_angle(h, heading_of(here, target)))
        base.update(distance_to_next_m=to_next, bearing_to_next_deg=bearing, next=nxt, off=self.miss > 0,
                    hazard=self.hazard_near)
        return base

    def state(self):
        if not self.route:
            return {"route": None, "unmapped": self.unmapped}
        nxt = self.ms[self.mi] if self.mi < len(self.ms) else None
        lv = self.live()
        room = self.g.rooms.get(self.room, {}).get("label") if self.room else None
        return {"route": self.route.to_dict(self.g), "progress": round(self.progress, 2),
                "remaining": round(max(0.0, self.route.length - self.progress), 2),
                "next": nxt and {k: nxt[k] for k in ("type", "text", "along", "cue") if k in nxt},
                "distance_to_next_m": lv["distance_to_next_m"], "bearing_to_next_deg": lv["bearing_to_next_deg"],
                "maneuvers": self.ms, "done": self.done, "reroutes": self.reroutes,
                "path": [[round(p[0], 3), round(p[1], 3), p[2]] for p in self.path],
                "doors": self.doors, "room": room, "unmapped": self.unmapped,
                "way_back": self.way_back() if self.unmapped else None}


def vibration(cue):
    return CUES.get(cue) if cue else None
