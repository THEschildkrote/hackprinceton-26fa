"""a point cloud for tests/synthetic.py: floors, ceilings, wall sheets, one sconce, one glass wall.

frame matches the model api: metres, z up. floor 1 at z=0, floor 2 at z=4 (panos sit 1.5 m up).
"""
import numpy as np

from tests.synthetic import building

FLOOR_Z = {"f1": 0.0, "f2": 4.0}
CEILING_H = 2.7
HEADER_H = 2.0   # door headers start here; must not count as an overhang
# a wall sconce on the lobby side of the lobby/kitchen wall, next to the L5-L6 walk edge
SCONCE = {"floor": "f1", "x0": 5.1, "x1": 5.5, "y0": 5.65, "y1": 6.0, "h0": 1.2, "h1": 1.5}
# the upper lobby/office wall is glass: no points at all
GLASS = {"floor": "f1", "a": (8.0, 3.0), "b": (8.0, 6.0)}

PASSABLE = {"opening", "door", "doorway", "entrance", "entry", "archway", "open", "passage"}


def _key(a, b):
    return tuple(sorted([(round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))]))


def _sheet(a, b, h0, h1, z0, step=0.05):
    L = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    t = np.linspace(0, 1, max(2, int(L / step) + 1))
    h = np.arange(h0, h1 + 1e-9, step)
    T, H = np.meshgrid(t, h)
    x = a[0] + T * (b[0] - a[0])
    y = a[1] + T * (b[1] - a[1])
    return np.stack([x.ravel(), y.ravel(), (z0 + H).ravel()], 1)


def walls(model, floor_id):
    """deduped (a, b, type) wall segments of one floor."""
    f = next(f for f in model["floors"] if f["id"] == floor_id)
    V = {v["id"]: (v["position"]["x"], v["position"]["y"]) for v in f["vertices"]}
    out, seen = [], set()
    for e in f["edges"]:
        a, b = (V[v["id"]] for v in e["vertices"][:2])
        k = _key(a, b)
        if k not in seen:
            seen.add(k)
            out.append((a, b, (e.get("type") or "wall").lower()))
    return out


def cloud():
    m = building()
    parts = []
    for fid, z0 in FLOOR_Z.items():
        f = next(f for f in m["floors"] if f["id"] == fid)
        xs = [v["position"]["x"] for v in f["vertices"]]
        ys = [v["position"]["y"] for v in f["vertices"]]
        gx, gy = np.meshgrid(np.arange(min(xs), max(xs) + 1e-9, 0.1), np.arange(min(ys), max(ys) + 1e-9, 0.1))
        for h in (0.0, CEILING_H):
            parts.append(np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, z0 + h)], 1))
        for a, b, t in walls(m, fid):
            if fid == GLASS["floor"] and _key(a, b) == _key(GLASS["a"], GLASS["b"]):
                continue
            if t in PASSABLE:
                parts.append(_sheet(a, b, HEADER_H, CEILING_H, z0))
            else:
                parts.append(_sheet(a, b, 0.0, CEILING_H, z0))
    s = SCONCE
    X, Y, H = np.meshgrid(np.arange(s["x0"], s["x1"] + 1e-9, 0.05), np.arange(s["y0"], s["y1"] + 1e-9, 0.05),
                          np.arange(s["h0"], s["h1"] + 1e-9, 0.05))
    parts.append(np.stack([X.ravel(), Y.ravel(), FLOOR_Z[s["floor"]] + H.ravel()], 1))
    return np.concatenate(parts).astype(np.float32)
