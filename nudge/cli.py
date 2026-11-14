"""python -m nudge.cli <sid|synthetic> [--from X] [--to Y] [--profile blind] [--alts] [--simulate] [--json out.json]"""
import argparse
import json
import math
import sys
from collections import Counter

from .fetch import load
from .geom import dist
from .instruct import maneuvers
from .navigator import Navigator
from .router import PROFILES, NoRoute, alternatives, plan, resolve_goal
from .sim import walk
from .walkgraph import build


def stats(g):
    fl = {f["id"]: f["label"] for f in g.floors}
    per = Counter(n.floor for n in g.nodes.values())
    print("nodes:", ", ".join(f"{fl.get(k, k)} {v}" for k, v in per.items()), f"(total {len(g.nodes)})")
    edges = list(g.edges())
    tags = Counter(t for e in edges for t in e.tags)
    print(f"edges: {len(edges)}", dict(tags) if tags else "")
    comp = Counter(g.components().values())
    print(f"components: {len(comp)} sizes {sorted(comp.values(), reverse=True)[:8]}")
    rooms = Counter(n.room_label for n in g.nodes.values() if n.room_label)
    print("rooms with panos:", dict(rooms))
    for w in g.warnings:
        print("warning:", w)


def pick_start(g, frm):
    if not frm:
        return next(iter(g.nodes))
    hit = resolve_goal(g, frm)
    if not hit:
        sys.exit(f"no pano matches {frm!r}")
    return sorted(hit)[0]


def farthest(g, start):
    # bfs over graph distance, ignoring profiles
    seen, frontier, best = {start: 0.0}, [start], start
    while frontier:
        u = frontier.pop()
        for v, e in g.adj[u].items():
            d = seen[u] + e.length
            if d < seen.get(v, math.inf):
                seen[v] = d
                frontier.append(v)
    return max(seen, key=seen.get)


def show_route(g, r):
    print(f"\n[{r.profile}] {len(r.nodes)} panos, {r.length:.1f} m, cost {r.cost:.1f}")
    print("  path:", " > ".join(g.nodes[n].label for n in r.nodes))
    for m in maneuvers(g, r):
        if m["text"]:
            print(f"  {m['along']:6.1f} m  [{m['cue']}] {m['text']}")


def simulate(g, start, goal, profile, wobble, drift_at):
    r = plan(g, start, goal, profile)
    pts = list(walk(g, r, step=0.5, wobble=wobble))
    if drift_at is not None and drift_at < len(pts):
        x, y, f, h = pts[drift_at]
        # shove 4 m to the walker's right and walk back from there
        dx, dy = 4 * math.sin(h), -4 * math.cos(h)
        pts = pts[:drift_at] + [(x + dx, y + dy, f, h)] * 3
    nav = Navigator(g, goal, profile)
    s = g.nodes[start]
    print(f"\nsimulating {profile} walk from {s.label} to {goal!r}, {len(pts)} positions")

    def emit(p, evs):
        for e in evs:
            cue = f"[{e['cue']}]" if e.get("cue") else ""
            print(f"  ({p[0]:6.2f},{p[1]:6.2f}) {p[2]:>4} {e['type']:8} {cue:14} {e['text']}")

    emit((s.x, s.y, s.floor), nav.start(s.x, s.y, s.floor, 0.0))
    for i, p in enumerate(pts):
        emit(p, nav.update(*p))
        if nav.done:
            break
    # after a drift, keep walking the new route
    if drift_at is not None and not nav.done and nav.route:
        for p in walk(g, nav.route, step=0.5):
            emit(p, nav.update(*p))
            if nav.done:
                break
    print(f"done={nav.done} reroutes={nav.reroutes}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nudge.cli")
    ap.add_argument("sid")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--profile", default="blind", choices=sorted(PROFILES))
    ap.add_argument("--alts", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--wobble", type=float, default=0.0)
    ap.add_argument("--drift-at", type=int)
    ap.add_argument("--json")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)

    model = load(a.sid, refresh=a.refresh)
    g = build(model)
    print(f"{model.get('name', a.sid)} ({a.sid})" + (f"  dropped fields: {model['_dropped']}" if model.get("_dropped") else ""))
    stats(g)

    routing = a.frm or a.to or a.alts or a.simulate or a.json
    if not routing:
        return
    start = pick_start(g, a.frm)
    goal = a.to or farthest(g, start)
    route = None
    try:
        if a.alts:
            for p, r in alternatives(g, start, goal, profiles=tuple(PROFILES)).items():
                show_route(g, r) if not isinstance(r, str) else print(f"\n[{p}] {r}")
        else:
            route = plan(g, start, goal, a.profile)
            show_route(g, route)
        if a.simulate:
            simulate(g, start, goal, a.profile, a.wobble, a.drift_at)
    except NoRoute as ex:
        print("no route:", ex)
    if a.json:
        out = g.to_dict()
        out["route"] = route.to_dict(g) if route else None
        out["maneuvers"] = maneuvers(g, route) if route else None
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f)
        print("wrote", a.json)


if __name__ == "__main__":
    main()
