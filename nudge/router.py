"""a* over the walk graph. profiles say what a user can't or would rather not do."""
import heapq
import itertools
from dataclasses import dataclass, field

from .geom import dist, heading, turn_angle

FLOOR_H = 4.0  # heuristic metres per floor change


@dataclass
class Profile:
    name: str
    avoid: set = field(default_factory=set)          # edge/node tags that are never used
    penalty: dict = field(default_factory=dict)      # tag -> extra metres per use
    turn_cost: float = 0.0                           # metres per 90 degrees of turning

    def describe(self):
        return {"name": self.name, "avoid": sorted(self.avoid), "penalty": self.penalty, "turn_cost": self.turn_cost}


PROFILES = {
    "default": Profile("default", avoid={"closed"},
                       penalty={"uncertain": 15, "vertical_unknown": 30, "hazard": 5, "narrow": 2, "glass_wall": 2}),
    # blind cane user: fewer turns, hazards are expensive, unsure links are worse than a detour
    "blind": Profile("blind", avoid={"closed"},
                     penalty={"hazard": 40, "uncertain": 40, "vertical_unknown": 60, "stairs": 8, "door": 1.5,
                              "room_change": 0.5, "glass_wall": 15, "narrow": 4, "step": 10},
                     turn_cost=2.0),
    "wheelchair": Profile("wheelchair", avoid={"stairs", "closed", "step"},
                          penalty={"hazard": 10, "uncertain": 50, "vertical_unknown": 200, "door": 3, "narrow": 25,
                                   "glass_wall": 3}),
}


@dataclass
class Route:
    nodes: list
    length: float
    cost: float
    profile: str

    def to_dict(self, g):
        return {"nodes": self.nodes, "length": round(self.length, 2), "cost": round(self.cost, 2),
                "profile": self.profile,
                "points": [[g.nodes[n].x, g.nodes[n].y, g.nodes[n].floor] for n in self.nodes]}


class NoRoute(Exception):
    pass


def resolve_goal(g, goal):
    """a pano id, a pano label, a room id, or a room label ('Kitchen'). returns a set of node ids."""
    if isinstance(goal, (set, list, tuple)):
        return set(goal)
    if goal in g.nodes:
        return {goal}
    by_label = {n.id for n in g.nodes.values() if n.label == goal}
    if by_label:
        return by_label
    by_room = {n.id for n in g.nodes.values() if n.room == goal}
    if by_room:
        return by_room
    low = str(goal).lower()
    by_room_label = {n.id for n in g.nodes.values() if n.room_label and n.room_label.lower() == low}
    if not by_room_label:
        by_room_label = {n.id for n in g.nodes.values() if n.room_label and low in n.room_label.lower()}
    if not by_room_label:
        by_room_label = {n.id for n in g.nodes.values() if any(low in str(l.get("text", "")).lower()
                                                                 for l in n.landmarks)}
    return by_room_label


def edge_cost(e, prof, blocked_edges=(), blocked_nodes=()):
    if (e.a, e.b) in blocked_edges or (e.b, e.a) in blocked_edges or e.a in blocked_nodes or e.b in blocked_nodes:
        return None
    if e.tags & prof.avoid:
        return None
    return e.length + sum(prof.penalty.get(t, 0) for t in e.tags)


def plan(g, start, goal, profile="default", blocked_edges=(), blocked_nodes=()):
    prof = PROFILES[profile] if isinstance(profile, str) else profile
    goals = resolve_goal(g, goal) - set(blocked_nodes)
    if start not in g.nodes:
        raise NoRoute(f"unknown start {start}")
    if not goals:
        raise NoRoute(f"no place matches {goal!r}")
    goal_nodes = [g.nodes[x] for x in goals]

    def h(nid):
        n = g.nodes[nid]
        return min(dist(n.xy, q.xy) + FLOOR_H * (n.floor != q.floor) * 0.5 for q in goal_nodes)

    # state is (node, previous node) so turning can cost something
    tie = itertools.count()
    start_state = (start, None)
    best = {start_state: 0.0}
    parent = {start_state: None}
    pq = [(h(start), next(tie), 0.0, start_state)]
    while pq:
        _, _, c, state = heapq.heappop(pq)
        if c > best.get(state, float("inf")) + 1e-9:
            continue
        u, prev = state
        if u in goals:
            path, s = [], state
            while s:
                path.append(s[0])
                s = parent[s]
            path.reverse()
            length = sum(g.edge(a, b).length for a, b in zip(path, path[1:]))
            return Route(path, length, c, prof.name)
        for v, e in g.adj[u].items():
            if v == prev:
                continue
            ec = edge_cost(e, prof, blocked_edges, blocked_nodes)
            if ec is None or (g.nodes[v].tags & prof.avoid and v not in goals):
                continue
            if prof.turn_cost and prev is not None and "vertical" not in e.tags:
                pe = g.edge(prev, u)
                if pe and "vertical" not in pe.tags:
                    ang = abs(turn_angle(heading(g.nodes[prev].xy, g.nodes[u].xy), heading(g.nodes[u].xy, g.nodes[v].xy)))
                    if ang > 20:
                        ec += prof.turn_cost * ang / 90
            nc = c + ec
            ns = (v, u)
            if nc < best.get(ns, float("inf")) - 1e-9:
                best[ns] = nc
                parent[ns] = state
                heapq.heappush(pq, (nc + h(v), next(tie), nc, ns))
    raise NoRoute(f"no {prof.name} route from {g.nodes[start].label} to {goal!r}")


def alternatives(g, start, goal, profiles=("blind", "wheelchair", "default"), **kw):
    """same trip under several profiles; what the exception agent compares."""
    out = {}
    for p in profiles:
        try:
            out[p] = plan(g, start, goal, p, **kw)
        except NoRoute as ex:
            out[p] = str(ex)
    return out
