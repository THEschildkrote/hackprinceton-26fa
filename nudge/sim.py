"""fake a person walking a route, for tests and the demo."""
import math
import random

from .geom import heading


def walk(g, route, step=0.5, wobble=0.0, seed=0):
    """yield (x, y, floor, heading) every `step` metres along the route, with optional sideways wobble."""
    rnd = random.Random(seed)
    ids = route.nodes
    h = 0.0
    for a, b in zip(ids, ids[1:]):
        na, nb = g.nodes[a], g.nodes[b]
        if na.floor != nb.floor:
            yield nb.x, nb.y, nb.floor, h
            continue
        h = heading(na.xy, nb.xy)
        L = math.dist(na.xy, nb.xy)
        n = max(1, int(L / step))
        for i in range(1, n + 1):
            t = i / n
            off = rnd.uniform(-wobble, wobble) if wobble else 0.0
            x = na.x + t * (nb.x - na.x) - off * math.sin(h)
            y = na.y + t * (nb.y - na.y) + off * math.cos(h)
            yield x, y, na.floor, h


def run(nav, positions, start=None):
    """drive a navigator; returns every event with the position it fired at."""
    out = []
    positions = list(positions)
    if start is None:
        start = positions[0]
    for e in nav.start(*start):
        out.append((start, e))
    for p in positions:
        for e in nav.update(*p):
            out.append((p, e))
        if nav.done:
            break
    return out
