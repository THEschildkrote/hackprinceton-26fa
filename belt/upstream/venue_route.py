"""Pick two points in a building the upstream server knows, plan the route there, and give it to the belt.

Run it on the laptop that has the building's scan (the upstream server's cache), with that server running:

    python venue_route.py                         two points on the venue's biggest floor, 6 to 12 m apart
    python venue_route.py --from p_A --to p_B     choose the two points yourself (ids from --list)
    python venue_route.py --list                  show the points on each floor
    python venue_route.py --dry                   plan and print, send nothing

    python venue_route.py --loop --floor "Floor 3"          the longest loop on that floor, back to its start
    python venue_route.py --loop --from p_A                 the longest loop through that point
    python venue_route.py --from p_A --via p_B p_C --loop   through those points in order, and back to p_A
    python venue_route.py --from p_A --via p_B --to p_C     through p_B on the way

    A loop as two walks, each an ordinary walk on the server (its dashboard follows both):
    python venue_route.py --from p_A --to p_B                              walk out: line up at p_A, begin
    python venue_route.py --from here --to p_A --other-way --continue      at p_B: back round the other way

    python venue_route.py --begin SESSION         the wearer is in place: line the belt up and start guiding

It needs nothing but Python. There are two ways the route reaches the belt, and it picks one itself:

  through the server   when the server has the belt hand-off (nudge/belt.py) and was started with
                       NUDGE_BELT_URL. The walk is started with /api/nav/start, the server sends the route
                       to the belt, and the dashboard, the speech and the caretaker follow the same trip.
  straight to the belt otherwise (or with --direct). The route from /api/route is posted to the belt.
                       The belt guides just the same; the server does not know a walk is on.

A planner only knows the shortest way between two points, and a loop is not one. So --loop and --via are
worked out here, on the planner's own graph (its points and the walkable edges between them, one floor):
each stretch is the shortest way that does not reuse a corridor already walked. A loop or a route with
via-points always goes straight to the belt, because the server's walk has one destination; the belt's
own viewer is then the live map. --save writes the route as a file the belt can keep in routes/.

A route that changes floor is walked one floor at a time; the belt waits at the change.

--continue is for a walk that follows another: the belt keeps its position and its calibration from the
walk before, takes the new route from where the wearer is standing, and starts guiding at once. Nobody
lines up again. --from here starts from the planner's point nearest to where the belt has the wearer.
--other-way asks for the way that is not the shortest: the planner would send the walk back from B to A
along the corridor just walked, and this makes it come round the other side, which closes a loop.

Then: the wearer stands on the first point, facing the second, with the phone streaming. Run --begin with
the session it printed, or press Calibrate and Start in the belt's viewer (http://<pi>:8000/viewer).
"""
import argparse
import json
import math
import random
import sys
import urllib.error
import urllib.request

VENUE_SID = "R1FX6EmdcDt"  # rabinowitz hall, the hackathon venue (nudge/buildings.py upstream)


def call(method, url, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local machines: never via a proxy
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        text = ex.read().decode(errors="replace")[:300]
        raise SystemExit(f"{method} {url} answered HTTP {ex.code}: {text}") from None
    except (urllib.error.URLError, OSError) as ex:
        raise SystemExit(f"cannot reach {url}: {getattr(ex, 'reason', ex)}") from None


def ask(method, url, body=None, timeout=5):
    """like call, but None instead of stopping: for things that may not be there."""
    try:
        return call(method, url, body, timeout)
    except SystemExit:
        return None


def floor_graph(graph, floor):
    """the planner's walkable edges on one floor: {point: [(neighbour, metres, [[x, y] on the way]), ...]}."""
    on_floor = {n["id"] for n in graph["nodes"] if n["floor"] == floor}
    adj = {i: [] for i in on_floor}
    for e in graph.get("edges", []):
        a, b = e.get("a"), e.get("b")
        if a in on_floor and b in on_floor:
            via = [v[:2] for v in e.get("via") or [] if isinstance(v, (list, tuple)) and len(v) >= 2]
            adj[a].append((b, float(e["length"]), via))
            adj[b].append((a, float(e["length"]), via[::-1]))
    return adj


def shortest(adj, start, goal, used_edges=(), closed=()):
    """the shortest way from start to goal (ids), not along `used_edges` and not through `closed` points.
    returns (metres, [ids]) or None."""
    import heapq
    used, closed = set(used_edges), set(closed) - {start, goal}
    best, queue, came = {start: 0.0}, [(0.0, start)], {}
    while queue:
        d, here = heapq.heappop(queue)
        if here == goal:
            path = [goal]
            while path[-1] != start:
                path.append(came[path[-1]])
            return d, path[::-1]
        if d > best.get(here, math.inf):
            continue
        for nxt, length, _ in adj.get(here, []):
            if nxt in closed or frozenset((here, nxt)) in used:
                continue
            if d + length < best.get(nxt, math.inf):
                best[nxt], came[nxt] = d + length, here
                heapq.heappush(queue, (d + length, nxt))
    return None


def through(adj, stops):
    """one walk through the stops in order, each stretch the shortest way that does not reuse a corridor
    already walked or pass through a point already visited. returns (metres, [ids])."""
    total, path, used = 0.0, [stops[0]], set()
    for a, b in zip(stops, stops[1:]):
        last = b == stops[-1]
        got = shortest(adj, a, b, used, closed=[p for p in path if not (last and p == b)])
        if got is None:
            raise SystemExit(f"no way from {a} to {b} that does not go back over the route so far")
        total += got[0]
        used |= {frozenset(pair) for pair in zip(got[1], got[1][1:])}
        path += got[1][1:]
    return total, path


def longest_loop(adj, starts, min_m, max_m):
    """the longest loop within min_m..max_m that leaves one of `starts` and comes back to it another way."""
    best = None
    for a in starts:
        for b in adj:
            if b == a:
                continue
            out = shortest(adj, a, b)
            if out is None:
                continue
            back = shortest(adj, b, a, {frozenset(pair) for pair in zip(out[1], out[1][1:])}, closed=out[1][1:-1])
            if back is None:
                continue
            total = out[0] + back[0]
            if min_m <= total <= max_m and (best is None or total > best[0] + 1e-9):
                best = (total, out[1] + back[1][1:])
    return best


def polyline(nodes, adj, path):
    """[[x, y, floor], ...] along a path of ids, with the bends the planner keeps on each edge."""
    first = nodes[path[0]]
    pts = [[first["x"], first["y"], first["floor"]]]
    for a, b in zip(path, path[1:]):
        via = next((v for n, _, v in adj[a] if n == b), [])
        pts += [[x, y, first["floor"]] for x, y in via]
        pts.append([nodes[b]["x"], nodes[b]["y"], nodes[b]["floor"]])
    return pts


def first_straight(points, bend_deg=20.0):
    """how far the route runs from its first point before it bends: what the wearer faces along at calibration."""
    run = 0.0
    for i in range(1, len(points)):
        (ax, ay), (bx, by) = points[i - 1][:2], points[i][:2]
        if i > 1:
            (px, py) = points[i - 2][:2]
            turn = math.degrees(math.atan2(by - ay, bx - ax) - math.atan2(ay - py, ax - px))
            if abs((turn + 180) % 360 - 180) > bend_deg:
                break
        run += math.dist((ax, ay), (bx, by))
    return run


def start_on_a_straight(nodes, adj, loop):
    """the same loop, begun at the point (and in the direction) with the longest straight ahead of it."""
    ring = loop[:-1]
    best = None
    for way in (ring, ring[::-1]):
        for i in range(len(way)):
            path = way[i:] + way[:i] + [way[i]]
            run = first_straight(polyline(nodes, adj, path))
            if best is None or run > best[0] + 1e-9:
                best = (run, path)
    return best[1]


def corners(points, min_deg=45.0):
    """how many real turns the route has on its first floor."""
    pts = [p for p in points if p[2] == points[0][2]]
    n = 0
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        turn = abs((math.degrees(h2 - h1) + 180) % 360 - 180)
        n += turn >= min_deg
    return n


def describe(node):
    where = node.get("room_label") or node.get("room") or "no room label"
    return f"{node['id']} ({node.get('label') or '?'}, {where}) at x={node['x']:.1f} y={node['y']:.1f}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan a route between two points and give it to the belt.")
    ap.add_argument("--server", default="http://localhost:8765", help="the upstream nudge server")
    ap.add_argument("--belt", default="http://172.20.10.2:8000", help="the belt runtime on the Pi")
    ap.add_argument("--sid", default=VENUE_SID, help="building scan id (default: the venue)")
    ap.add_argument("--from", dest="start", help="id of the point to start on, or 'here': where the belt has the wearer")
    ap.add_argument("--other-way", action="store_true",
                    help="not the shortest way but the next one, avoiding every corridor of the shortest")
    ap.add_argument("--continue", dest="carry_on", action="store_true",
                    help="this walk follows another: no lining up, the belt carries on from where the wearer is")
    ap.add_argument("--to", dest="goal", help="id of the point to walk to")
    ap.add_argument("--floor", help="floor id to pick points on (default: the floor with the most points)")
    ap.add_argument("--via", nargs="+", metavar="ID", default=[], help="points to pass through, in order")
    ap.add_argument("--loop", action="store_true",
                    help="come back to the start; with no --via, the longest loop on the floor (through --from)")
    ap.add_argument("--min", dest="min_m", type=float, help="shortest route to accept, metres (6; loops 20)")
    ap.add_argument("--max", dest="max_m", type=float, help="longest route to accept, metres (12; loops 150)")
    ap.add_argument("--name", help="a name for the route (shown in the belt's viewer, and used by --save)")
    ap.add_argument("--keep", action="store_true",
                    help="also keep the route on the belt under --name: it then loads from the belt's own "
                         "viewer with no server or laptop, and survives a restart")
    ap.add_argument("--profile", default="blind")
    ap.add_argument("--seed", type=int, default=1, help="change it to get a different pair")
    ap.add_argument("--list", action="store_true", help="list the points and stop")
    ap.add_argument("--dry", action="store_true", help="do not send the route to the belt")
    ap.add_argument("--direct", action="store_true", help="post the route to the belt, not through the server")
    ap.add_argument("--begin", metavar="SESSION", help="calibrate and start the belt for this server session")
    ap.add_argument("--traveller", metavar="NAME",
                    help="through the server: register the wearer under this name and print the code a "
                         "caretaker follows them with (what the traveller's phone app would otherwise do)")
    ap.add_argument("--save", help="also write the planned route to this file")
    a = ap.parse_args(argv)
    server, belt = a.server.rstrip("/"), a.belt.rstrip("/")
    looping = a.loop or bool(a.via)
    if a.min_m is None:
        a.min_m = 20.0 if a.loop else 6.0
    if a.max_m is None:
        a.max_m = 150.0 if a.loop else 12.0

    if a.begin:
        out = call("POST", f"{server}/api/belt/begin", {"session": a.begin})
        if not out.get("ok"):
            raise SystemExit("the belt did not start: "
                             + (out.get("error") or f"session {a.begin} does not hold the belt (it has {out.get('session')})"))
        print("belt      lined up and guiding")
        return 0

    graph = call("GET", f"{server}/api/graph/{a.sid}")
    nodes = {n["id"]: n for n in graph["nodes"]}
    labels = {f["id"]: f.get("label") or f["id"] for f in graph.get("floors", [])}
    by_floor = {}
    for n in nodes.values():
        by_floor.setdefault(n["floor"], []).append(n)

    if a.list:
        for floor, items in by_floor.items():
            print(f"{labels.get(floor, floor)}  [{floor}]  {len(items)} points")
            for n in sorted(items, key=lambda n: (n["x"], n["y"])):
                print("   " + describe(n))
        return 0

    def plan(start, goal):
        return call("POST", f"{server}/api/route", {"sid": a.sid, "from": start, "to": goal, "profile": a.profile})

    def floor_id(wanted):
        """--floor takes the id or the name the building gives it ("Floor 3")."""
        if wanted in by_floor:
            return wanted
        named = [f for f, label in labels.items() if str(label).lower() == str(wanted).lower() and f in by_floor]
        if not named:
            raise SystemExit(f"no floor {wanted}; floors are " + ", ".join(f"{labels.get(f, f)} [{f}]" for f in by_floor))
        return named[0]

    if a.start == "here":
        seen = call("GET", f"{belt}/state", timeout=5)
        pose = seen.get("pose")
        if not pose:
            raise SystemExit("the belt has not been lined up, so it does not know where the wearer is: give --from an id")
        on = (seen.get("route") or {}).get("floor")
        pool = by_floor.get(on) or (by_floor[floor_id(a.floor)] if a.floor else list(nodes.values()))
        near = min(pool, key=lambda n: math.dist((n["x"], n["y"]), (pose["x"], pose["y"])))
        off = math.dist((near["x"], near["y"]), (pose["x"], pose["y"]))
        print(f"here      the belt has the wearer at x={pose['x']:.1f} y={pose['y']:.1f}; "
              f"the nearest point is {describe(near)}, {off:.1f} m away")
        a.start = near["id"]

    avoid = []
    if looping:
        # worked out here on the planner's graph: see the top of this file
        for name in [a.start, a.goal, *a.via]:
            if name and name not in nodes:
                raise SystemExit(f"no point called {name}; see --list")
        anchor = a.start or (a.via[0] if a.via else None)
        floor = nodes[anchor]["floor"] if anchor else floor_id(a.floor or max(by_floor, key=lambda f: len(by_floor[f])))
        adj = floor_graph(graph, floor)
        other = [n for n in [a.start, a.goal, *a.via] if n and nodes[n]["floor"] != floor]
        if other:
            raise SystemExit(f"{', '.join(other)} not on {labels.get(floor, floor)}: a loop or via-route stays on one floor")
        if a.via or a.goal:
            if not a.start:
                raise SystemExit("--via needs --from: the point to start on")
            stops = [a.start, *a.via] + ([a.goal] if a.goal else []) + ([a.start] if a.loop else [])
            if len(stops) < 2 or (a.goal and a.loop and a.goal == a.start):
                raise SystemExit("give --to, or --loop to come back to the start")
            length, path = through(adj, stops)
        else:
            found = longest_loop(adj, [a.start] if a.start else sorted(adj), a.min_m, a.max_m)
            if found is None:
                raise SystemExit(f"no loop of {a.min_m:g} to {a.max_m:g} m on {labels.get(floor, floor)}"
                                 + (f" through {a.start}" if a.start else "") + "; try --min/--max, --from or --via")
            length, path = found
            if not a.start:
                path = start_on_a_straight(nodes, adj, path)  # calibration is by facing along the first leg
        answer = {"route": {"nodes": path, "length": length, "profile": "loop" if a.loop else "via",
                            "points": polyline(nodes, adj, path)}, "maneuvers": []}
        a.direct = True
    elif a.start and a.goal:
        for name in (a.start, a.goal):
            if name not in nodes:
                raise SystemExit(f"no point called {name}; see --list")
        answer = plan(a.start, a.goal)
        if a.other_way:
            short = answer["route"]["nodes"]
            avoid = [list(pair) for pair in zip(short, short[1:])]
            taken = {frozenset(pair) for pair in avoid}
            again = call("POST", f"{server}/api/route", {"sid": a.sid, "from": a.start, "to": a.goal,
                                                         "profile": a.profile, "avoid": avoid})
            if taken & {frozenset(pair) for pair in zip(again["route"]["nodes"], again["route"]["nodes"][1:])}:
                # a server that does not know "avoid" yet gives the same route back: work it out here instead
                adj = floor_graph(graph, nodes[a.start]["floor"])
                found = shortest(adj, a.start, a.goal, taken)
                if found is None:
                    raise SystemExit(f"there is no other way from {a.start} to {a.goal} on this floor")
                again = {"route": {"nodes": found[1], "length": found[0], "profile": "other way",
                                   "points": polyline(nodes, adj, found[1])}, "maneuvers": []}
                a.direct = True
                print("note      this server does not know \"avoid\" yet, so the other way was worked out here "
                      "and goes straight to the belt")
            answer = again
    else:
        floor = floor_id(a.floor or max(by_floor, key=lambda f: len(by_floor[f])))
        items = sorted(by_floor[floor], key=lambda n: n["id"])
        pairs = [(p, q) for p in items for q in items if p["id"] != q["id"]
                 and a.min_m * 0.7 <= math.dist((p["x"], p["y"]), (q["x"], q["y"])) <= a.max_m]
        random.Random(a.seed).shuffle(pairs)
        answer, fallback = None, None
        for p, q in pairs[:40]:  # a few tries; each is one quick request
            got = plan(p["id"], q["id"])
            r = got["route"]
            one_floor = all(pt[2] == r["points"][0][2] for pt in r["points"])
            if not one_floor or not a.min_m <= r["length"] <= a.max_m:
                continue
            if corners(r["points"]) >= 1:  # a walk with a turn in it shows more than a straight line
                answer = got
                break
            fallback = fallback or got
        answer = answer or fallback
        if answer is None:
            raise SystemExit(f"no one-floor route of {a.min_m:g} to {a.max_m:g} m found on {labels.get(floor, floor)}; "
                             "try --min/--max, --seed, or --from/--to")

    route = answer["route"]
    start, goal = nodes[route["nodes"][0]], nodes[route["nodes"][-1]]
    print(f"building {a.sid}, floor {labels.get(start['floor'], start['floor'])}")
    print("start on  " + describe(start))
    print(("back to   " if start["id"] == goal["id"] else "walk to   ") + describe(goal))
    print(f"route     {route['length']:.1f} m, {len(route['points'])} points, {corners(route['points'])} turn(s)"
          + (f", about {round(route['length'] / 0.5)} steps of half a metre" if looping else ""))
    if looping:
        pts, legs = route["points"], []
        for i in range(1, len(pts)):
            legs.append(math.dist(pts[i - 1][:2], pts[i][:2]))
        print(f"through   {' > '.join(route['nodes'])}")
        print(f"legs      longest {max(legs):.1f} m, shortest {min(legs):.1f} m")
    for m in answer.get("maneuvers", [])[:8]:
        if isinstance(m, dict) and m.get("text"):
            print("   say: " + m["text"])
    if len(route["points"]) > 1:
        (x0, y0, _), (x1, y1, _) = route["points"][0], route["points"][1]
        print(f"face      from the start towards x={x1:.1f} y={y1:.1f} ({math.dist((x0, y0), (x1, y1)):.1f} m away)")
        if first_straight(route["points"]) < 2.0:
            print("warning   the route bends within 2 m of its start. The belt lines itself up by having the wearer "
                  "face along the first stretch, so start somewhere with a straight ahead (--from)")
    if a.name:
        answer["name"] = a.name
    if a.save:
        with open(a.save, "w", encoding="utf-8") as fh:
            if looping:  # the belt's own route format: copy it into routes/ on the Pi to keep it
                json.dump({"name": a.name or ("loop" if a.loop else "route"),
                           "waypoints": [p[:2] for p in route["points"]]}, fh, indent=1)
            else:
                json.dump(answer, fh, indent=1)
        print("saved     " + a.save)
    if a.dry:
        print("dry run: nothing sent to the belt")
        return 0

    handoff = None if a.direct else ask("GET", f"{server}/api/belt")
    if handoff and handoff.get("enabled"):
        # the server sends the route to the belt itself and follows the belt's position from here on
        if (handoff.get("belt") or {}).get("state") == "NAVIGATING" and not a.carry_on:
            raise SystemExit("the belt is guiding someone right now: press Stop in its viewer first")
        who = {}
        if a.traveller:
            # the same name always gets the same id, so the code stays the same from one walk to the next
            tid = "belt-" + ("".join(c for c in a.traveller.lower() if c.isalnum())[:16] or "wearer")
            reg = call("POST", f"{server}/api/traveller/register", {"traveller_id": tid, "name": a.traveller})
            who = {"traveller": reg["traveller_id"]}
            print(f"traveller {a.traveller}; a caretaker follows with code {reg['code']} "
                  f"(text \"follow {reg['code']}\" to the iMessage line, or type it into the caretaker app)")
        started = call("POST", f"{server}/api/nav/start",
                       {"sid": a.sid, "to": goal["id"], "profile": a.profile,
                        "x": start["x"], "y": start["y"], "floor": start["floor"], **who,
                        **({"avoid": avoid} if avoid else {})})
        session = started["session"]
        now = call("GET", f"{server}/api/belt")
        took = now.get("route")
        if not took:
            raise SystemExit(f"the server started session {session}, but the belt at {now.get('url')} did not take "
                             f"the route: {now.get('error') or 'no reason given'}")
        print(f"server    session {session}; the dashboard at {server}/ follows this walk")
        print(f"belt      at {now.get('url')} took {took.get('used')} of {took.get('given')} points"
              + (f", dropped {took['dropped_other_floors']} on other floors" if took.get("dropped_other_floors") else ""))
        if a.carry_on and now.get("continued"):
            out = call("POST", f"{server}/api/belt/begin", {"session": session})
            if not out.get("ok"):
                raise SystemExit("the belt took the route but did not start: " + str(out.get("error")))
            print("belt      carried on from where the wearer stands: guiding, nobody lined up again")
            return 0
        if a.carry_on:
            print("note      the belt did not carry on (it has not been lined up, the wearer is not within 3 m of "
                  "this walk's start, or the server is older than pull request 3): line up as for a first walk")
        print("next      stand on the start, face the second point, then run")
        print(f"             python {sys.argv[0]} --server {server} --begin {session}")
        print(f"          or press Calibrate and Start at {now.get('url')}/viewer")
        return 0

    if looping:
        print("note      a loop or a via-route goes straight to the belt (the server's walk has one destination): "
              "the belt's viewer is the live map for this one")
    elif not a.direct:
        print("note      this server has no belt hand-off switched on (NUDGE_BELT_URL), so the route goes "
              "straight to the belt and the server's dashboard will not show the walk")
    if a.traveller:
        print("note      --traveller only works through the server; nobody is registered")
    state = call("GET", f"{belt}/state", timeout=5)
    if a.carry_on:
        # the belt keeps its position and calibration, takes the route from where the wearer is, and goes
        reply = call("POST", f"{belt}/route", {**answer, "reroute": True}, timeout=5)
        if not reply.get("guiding"):
            call("POST", f"{belt}/start", timeout=5)
        print(f"belt      took {reply.get('used')} of {reply.get('given')} points and carried on from where the "
              "wearer stands: guiding, nobody lined up again")
        if a.keep:
            print("note      --keep is for a route with its own start; a continuation is not kept")
        return 0
    if state.get("state") == "NAVIGATING":
        raise SystemExit("the belt is guiding someone right now: press Stop in its viewer first")
    reply = call("POST", f"{belt}/route", answer, timeout=5)
    if a.keep:
        kept = call("POST", f"{belt}/route/save", {"name": a.name or ("loop" if a.loop else "route")}, timeout=5)
        print(f"kept      on the belt as {kept['name']}: pick it in the viewer's route list from now on, no server needed")
    print(f"belt      took {reply.get('used')} of {reply.get('given')} points"
          + (f", dropped {reply['dropped_other_floors']} on other floors" if reply.get("dropped_other_floors") else ""))
    print(f"next      stand on the start, face the second point, then Calibrate and Start at {belt}/viewer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
