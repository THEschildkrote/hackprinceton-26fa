"""live guidance: feed it positions, it tells you what to say and buzz. no model in the loop.

positions are in the model frame (metres). in the browser demo they come from the showcase sdk camera;
on a phone they would come from whatever localizer we plug in.
"""
from .geom import dist, heading as heading_of, project, turn_angle
from .instruct import CUES, feet, maneuvers, orient
from .router import NoRoute, plan

SNAP_RADIUS = 6.0
LOOKAHEAD_M = 1.5  # point on the route we steer the user toward
START_SLACK_M = 2.0  # how much farther than the nearest pano a start pano may be


def snap(g, x, y, floor):
    """nearest pano you can walk to in a straight line; falls back to nearest on the floor."""
    cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.floor == floor), key=lambda t: t[0])
    if not cands:
        cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values()), key=lambda t: t[0])
    for d, n in cands:
        if d > SNAP_RADIUS:
            break
        if not g.blocked_line((x, y), n.xy, n.floor):
            return n.id, d
    return cands[0][1].id, cands[0][0]


def visible(g, x, y, floor, k=4):
    """up to k nearest panos on this floor with a clear straight line from (x, y)."""
    cands = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.floor == floor), key=lambda t: t[0])
    out = [(n.id, d) for d, n in cands if d <= SNAP_RADIUS and not g.blocked_line((x, y), n.xy, floor)]
    return out[:k]


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

    # route bookkeeping

    def _start_node(self, x, y, floor):
        """the nearby pano that makes the whole trip shortest, so a reroute never walks you backwards first."""
        best = None
        near = visible(self.g, x, y, floor)
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
        return best[1] if best else snap(self.g, x, y, floor)[0]

    def _set_route(self, start_node):
        self.route = plan(self.g, start_node, self.goal, self.profile, self.blocked_edges, self.blocked_nodes)
        self.ms = maneuvers(self.g, self.route)
        self.cum = [0.0]
        for a, b in zip(self.route.nodes, self.route.nodes[1:]):
            self.cum.append(self.cum[-1] + self.g.edge(a, b).length)
        self.progress = 0.0
        self.mi = 1 if len(self.ms) > 1 else 0   # next maneuver to reach (0 is depart)
        self.said = set()
        self.warned = set()
        self.miss = 0

    def _summary(self):
        turns = sum(1 for m in self.ms if m["type"] in ("turn", "bear", "uturn"))
        floors = sum(1 for m in self.ms if m["type"] == "floor")
        s = f"Route ready: about {feet(self.route.length)} feet, {turns} turn{'s' * (turns != 1)}"
        if floors:
            s += f", {floors} floor change{'s' * (floors != 1)}"
        return s + "."

    def _locate(self, x, y, floor):
        """(along, cross, clear) of the best spot on the route for this position."""
        best = None
        nodes = self.route.nodes
        for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
            na, nb = self.g.nodes[a], self.g.nodes[b]
            if "vertical" in self.g.edge(a, b).tags:
                if nb.floor == floor and dist((x, y), nb.xy) < self.off_m:
                    cand = (self.cum[i + 1], dist((x, y), nb.xy), nb.xy)
                else:
                    continue
            elif na.floor != floor:
                continue
            else:
                t, c, d = project((x, y), na.xy, nb.xy)
                cand = (self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), d, c)
            # stay near where we were, so loops in the route don't make us jump
            score = cand[1] + 0.05 * max(0.0, self.progress - cand[0]) + 0.02 * max(0.0, cand[0] - self.progress - 10)
            if best is None or score < best[0]:
                best = (score, cand)
        if best is None:
            return None
        along, cross, c = best[1]
        clear = cross < 0.05 or not self.g.blocked_line((x, y), c, floor)
        return along, cross, clear

    # public api

    def _seen(self, x, y, floor, heading):
        self.pos = (x, y, floor)
        if heading is not None:
            self.heading = heading

    def start(self, x, y, floor, heading=None):
        self._seen(x, y, floor, heading)
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

    def update(self, x, y, floor, heading=None):
        self._seen(x, y, floor, heading)
        if self.done or self.route is None:
            return []
        loc = self._locate(x, y, floor)
        if loc is None or loc[1] > self.off_m or not loc[2]:
            self.miss += 1
            if self.miss >= self.off_hits:
                return self._reroute(x, y, floor, heading, "You are off the route.")
            return []
        self.miss = 0
        along, _, _ = loc
        self.progress = max(self.progress, along) if along - self.progress < 15 else along
        ev = []

        # hazards just ahead on the route
        for i, (a, b) in enumerate(zip(self.route.nodes, self.route.nodes[1:])):
            e = self.g.edge(a, b)
            for h in e.hazards:
                key = (h.get("x"), h.get("y"))
                if key in self.warned:
                    continue
                if dist((x, y), (h["x"], h["y"])) < 3.5 and self.cum[i + 1] >= self.progress:
                    self.warned.add(key)
                    ev.append({"type": "hazard", "cue": "hazard",
                               "text": f"Caution: {h.get('note') or h.get('kind', 'obstacle').replace('_', ' ')} ahead."})

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
                    ev.append({"type": "soon", "text": f"In {feet(to_go)} feet, {verb}.", "cue": None})
            break
        return ev

    def _reroute(self, x, y, floor, heading, why):
        node = self._start_node(x, y, floor)
        self.reroutes += 1
        try:
            self._set_route(node)
        except NoRoute as ex:
            return [{"type": "no_route", "text": f"{why} I can't find another way. {ex}", "cue": "off_route"}]
        ev = [{"type": "reroute", "text": f"{why} New route, about {feet(self.route.length)} feet.", "cue": "off_route"}]
        if heading is not None and self.ms[0]["type"] == "depart":
            t, cue = orient(heading, self.ms[0]["heading"])
            ev.append({"type": "orient", "text": t, "cue": cue})
        return ev + self._announce_leg()

    def block(self, x, y, floor, a, b=None, heading=None, why="That way is blocked."):
        """exception hook: a door is locked or an elevator is out. block a node, or the edge a-b, and replan."""
        if b is None:
            self.blocked_nodes.add(a)
        else:
            self.blocked_edges.add((a, b))
        return self._reroute(x, y, floor, heading, why)

    def _point_at(self, along):
        """(x, y) on the route at `along` metres, not past the next floor change."""
        nodes = self.route.nodes
        for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
            na, nb = self.g.nodes[a], self.g.nodes[b]
            if "vertical" in self.g.edge(a, b).tags:
                if along < self.cum[i + 1]:
                    return na.xy  # don't steer through the elevator; stop at its door
                continue
            if self.cum[i + 1] >= along:
                L = self.cum[i + 1] - self.cum[i]
                t = 0.0 if L == 0 else (along - self.cum[i]) / L
                return (na.x + t * (nb.x - na.x), na.y + t * (nb.y - na.y))
        last = self.g.nodes[nodes[-1]]
        return last.xy

    def live(self, heading=None):
        """numbers for a haptic device. metres to the next maneuver, and the signed angle (deg, + = left)
        from the user's heading to the way they should walk right now."""
        if not self.route:
            return {"distance_to_next_m": None, "bearing_to_next_deg": None, "next": None,
                    "off": False, "done": self.done}
        h = self.heading if heading is None else heading
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
        return {"distance_to_next_m": to_next, "bearing_to_next_deg": bearing, "next": nxt,
                "off": self.miss > 0, "done": self.done}

    def state(self):
        if not self.route:
            return {"route": None}
        nxt = self.ms[self.mi] if self.mi < len(self.ms) else None
        lv = self.live()
        return {"route": self.route.to_dict(self.g), "progress": round(self.progress, 2),
                "remaining": round(max(0.0, self.route.length - self.progress), 2),
                "next": nxt and {k: nxt[k] for k in ("type", "text", "along", "cue") if k in nxt},
                "distance_to_next_m": lv["distance_to_next_m"], "bearing_to_next_deg": lv["bearing_to_next_deg"],
                "maneuvers": self.ms, "done": self.done, "reroutes": self.reroutes}


def vibration(cue):
    return CUES.get(cue) if cue else None
