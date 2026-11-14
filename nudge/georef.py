"""put a matterport model's frame on the map.

the model api gives one lat/lon per model; the frame's rotation is unknown. we register the scan's
outer outline (hull of the ground floor's wall vertices) against the building footprint (vexcel, or osm outside
its coverage) in local east/north metres: grid search rotation, align centroids, refine with point-to-segment
icp, score with a truncated symmetric chamfer distance. symmetric shapes (rectangles) stay ambiguous; we say so.
a scan of only part of a big building fails that fit; register_partial then fits its walls inside the footprint.
"""
import math

import numpy as np

from .mp import M_PER_DEG

OK_RMSE_M = 1.5  # scan-to-footprint fit counted as good


class ENU:
    """local east/north metres around an origin. fine over a city block."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.kx = M_PER_DEG * math.cos(math.radians(self.lat0))

    def to_enu(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG)

    def to_lonlat(self, e, n):
        return (self.lon0 + e / self.kx, self.lat0 + n / M_PER_DEG)


def hull(pts):
    """convex hull, counter-clockwise (monotone chain)."""
    p = sorted(set(map(tuple, pts)))
    if len(p) < 3:
        return p

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo, hi = [], []
    for q in p:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(p):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 0:
            hi.pop()
        hi.append(q)
    return lo[:-1] + hi[:-1]


def outline(g, floor=None):
    """outer outline of a scan: hull of the wall vertices on the ground floor (or the given floor)."""
    floors = g.floors
    f = next((f for f in floors if f["id"] == floor), None) if floor else (floors[0] if floors else None)
    pts = [w[0] for w in f["walls"]] + [w[1] for w in f["walls"]] if f and f["walls"] else []
    if len(pts) < 3:  # no walls: fall back to pano positions
        pts = [n.xy for n in g.nodes.values() if not f or n.floor == f["id"]]
    return hull(pts)


def sample_ring(ring, step=0.25):
    ring = list(ring)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    out = []
    for a, b in zip(ring, ring[1:]):
        L = math.dist(a, b)
        n = max(1, int(L / step))
        for i in range(n):
            t = i / n
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return np.array(out)


def _segments(ring):
    ring = list(ring)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    a = np.array(ring[:-1], float)
    b = np.array(ring[1:], float)
    return a, b


def closest_on_segments(P, A, B):
    """for each point in P (n,2), the closest point on segments A->B (m,2). returns (points, distances)."""
    d = B - A
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((P[:, None, :] - A[None]) * d[None]).sum(2) / L2[None], 0, 1)
    C = A[None] + t[..., None] * d[None]
    D = np.linalg.norm(P[:, None, :] - C, axis=2)
    j = D.argmin(1)
    return C[np.arange(len(P)), j], D[np.arange(len(P)), j]


def _rot(theta):
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


def _chamfer(P, Q, cap=3.0):
    """truncated symmetric chamfer between point sets (brute force; a few hundred points each)."""
    D = np.linalg.norm(P[:, None, :] - Q[None], axis=2)
    return 0.5 * (np.minimum(D.min(1), cap).mean() + np.minimum(D.min(0), cap).mean())


def _icp(P, A, B, R, t, iters=40, trim=1.5):
    """point-to-segment icp: P model samples, A->B footprint segments. returns R, t, rmse, inlier_frac."""
    for _ in range(iters):
        X = P @ R.T + t
        C, d = closest_on_segments(X, A, B)
        keep = d < trim
        if keep.sum() < 3:
            break
        p, q = P[keep], C[keep]
        pm, qm = p.mean(0), q.mean(0)
        H = (p - pm).T @ (q - qm)
        U, _, Vt = np.linalg.svd(H)
        Rn = Vt.T @ U.T
        if np.linalg.det(Rn) < 0:  # no reflections here
            Vt[-1] *= -1
            Rn = Vt.T @ U.T
        tn = qm - pm @ Rn.T
        done = np.abs(Rn - R).max() < 1e-7 and np.abs(tn - t).max() < 1e-5
        R, t = Rn, tn
        if done:
            break
    X = P @ R.T + t
    _, d = closest_on_segments(X, A, B)
    return R, t, float(np.sqrt((d ** 2).mean())), float((d < 0.75).mean())


class Georef:
    """model frame <-> local enu <-> lon/lat for one scan."""

    def __init__(self, enu, theta_deg, tx, ty, rmse_m=0.0, inlier_frac=1.0, ok=True, mirrored=False,
                 ambiguous=False, alternatives=(), source="footprint"):
        self.enu, self.theta_deg, self.tx, self.ty = enu, theta_deg, tx, ty
        self.rmse_m, self.inlier_frac, self.ok = rmse_m, inlier_frac, ok
        self.mirrored, self.ambiguous, self.alternatives, self.source = mirrored, ambiguous, list(alternatives), source
        th = math.radians(theta_deg)
        self._c, self._s = math.cos(th), math.sin(th)

    def model_to_enu(self, x, y):
        if self.mirrored:
            y = -y
        return (self._c * x - self._s * y + self.tx, self._s * x + self._c * y + self.ty)

    def enu_to_model(self, e, n):
        e, n = e - self.tx, n - self.ty
        x, y = self._c * e + self._s * n, -self._s * e + self._c * n
        return (x, -y) if self.mirrored else (x, y)

    def model_to_lonlat(self, x, y):
        return self.enu.to_lonlat(*self.model_to_enu(x, y))

    def lonlat_to_model(self, lon, lat):
        return self.enu_to_model(*self.enu.to_enu(lon, lat))

    def heading_model_from_compass(self, compass_deg):
        """compass degrees (clockwise from north) -> heading radians in the model frame (ccw from +x)."""
        h_enu = math.radians(90.0 - compass_deg)
        return h_enu - math.radians(self.theta_deg) if not self.mirrored else -(h_enu - math.radians(self.theta_deg))

    def to_dict(self):
        return {"theta_deg": round(self.theta_deg, 2), "tx": round(self.tx, 3), "ty": round(self.ty, 3),
                "rmse_m": round(self.rmse_m, 3), "inlier_frac": round(self.inlier_frac, 3), "ok": self.ok,
                "mirrored": self.mirrored, "ambiguous": self.ambiguous, "alternatives": self.alternatives,
                "source": self.source, "origin": [self.enu.lat0, self.enu.lon0] if self.enu else None,
                **({"score": self.score, "outside_frac": self.outside_frac} if hasattr(self, "score") else {})}


def register(model_outline, footprint_enu, step_deg=2.0, enu=None, try_mirror=None):
    """align a model-frame outline to a footprint ring in enu metres."""
    P0 = sample_ring(model_outline)
    Q = sample_ring(footprint_enu)
    A, B = _segments(footprint_enu)
    pc, qc = P0.mean(0), Q.mean(0)

    def search(P):
        scores = []
        for k in range(int(round(360 / step_deg))):
            th = math.radians(k * step_deg)
            R = _rot(th)
            t = qc - pc @ R.T
            scores.append((_chamfer(P @ R.T + t, Q), th))
        scores.sort()
        # refine the best few distinct starts; keep the best after icp
        starts, out = [], []
        for s, th in scores:
            if all(abs(((th - u) + math.pi) % (2 * math.pi) - math.pi) > math.radians(20) for u in starts):
                starts.append(th)
            if len(starts) == 4:
                break
        for th in starts:
            R = _rot(th)
            R, t, rmse, inl = _icp(P, A, B, R, qc - pc @ R.T)
            out.append((rmse + 2.0 * (1 - inl), rmse, inl, R, t))
        out.sort(key=lambda r: r[0])
        return out

    res = search(P0)
    mirrored = False
    if try_mirror or (try_mirror is None and res[0][1] > 1.0):
        Pm = P0 * np.array([1, -1])
        resm = search(Pm)
        if resm[0][0] < res[0][0] * 0.7:
            res, mirrored = resm, True
    best = res[0]
    _, rmse, inl, R, t = best
    theta = math.degrees(math.atan2(R[1, 0], R[0, 0])) % 360
    alts = []
    for r in res[1:]:
        th = math.degrees(math.atan2(r[3][1, 0], r[3][0, 0])) % 360
        # starts that converged onto the best answer are not alternatives
        if abs((th - theta + 180) % 360 - 180) < 10 or any(abs((th - a["theta_deg"] + 180) % 360 - 180) < 10 for a in alts):
            continue
        alts.append({"theta_deg": round(th, 1), "rmse_m": round(r[1], 3), "inlier_frac": round(r[2], 3)})
    ambiguous = any(a["rmse_m"] < rmse * 1.25 + 0.15 and a["inlier_frac"] > inl - 0.1 for a in alts)
    # footprints are traced from aerials and scans miss eaves and porches; ~1.5 m is a good fit
    ok = rmse <= OK_RMSE_M and inl >= 0.6
    return Georef(enu, theta, float(t[0]), float(t[1]), rmse, inl, ok, mirrored, ambiguous,
                  [a for a in alts if a["rmse_m"] < 2 * rmse + 0.5])


def _seg_dist(G, A, B):
    """distance from each point in G to the nearest of segments A->B (looped per segment; low memory)."""
    best = np.full(len(G), np.inf)
    for a, b in zip(A, B):
        d = b - a
        t = np.clip(((G - a) @ d) / max(d @ d, 1e-12), 0, 1)
        best = np.minimum(best, np.linalg.norm(G - (a + t[:, None] * d), axis=1))
    return best


def _inside(G, ring):
    ring = np.asarray(ring, float)
    x, y = G[:, 0], G[:, 1]
    res = np.zeros(len(G), bool)
    for (xi, yi), (xj, yj) in zip(ring, np.roll(ring, 1, axis=0)):
        if yi == yj:
            continue
        res ^= ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
    return res


def floor_walls(g, floor=None):
    f = next((f for f in g.floors if f["id"] == floor), None) if floor else (g.floors[0] if g.floors else None)
    return [(tuple(w[0]), tuple(w[1])) for w in (f or {}).get("walls") or []]


def _sample_segments(segs, step=0.5):
    pts = []
    for a, b in segs:
        a, b = np.asarray(a, float), np.asarray(b, float)
        n = max(1, int(np.linalg.norm(b - a) / step))
        pts += [a + (b - a) * i / n for i in range(n + 1)]
    return np.array(pts)


def _axis_hist(segs):
    """length-weighted wall directions mod 90 degrees, 1-degree bins."""
    h = np.zeros(90)
    for a, b in segs:
        dx, dy = b[0] - a[0], b[1] - a[1]
        h[int(math.degrees(math.atan2(dy, dx)) % 90) % 90] += math.hypot(dx, dy)
    return h / max(h.sum(), 1e-9)


def _peak_share(h):
    i = int(h.argmax())
    return sum(h[(i + d) % 90] for d in range(-2, 3))


def register_partial(walls_model, footprint_enu, enu=None, cell=0.5):
    """a scan that covers part of a building, so its hull is not the footprint (rabinowitz hall: a cross of
    corridors inside an h-shaped footprint). every wall should sit inside the footprint, as many as possible on
    its edge. rotation: square buildings line their walls up with the footprint's edges (mod 90), so only those
    four turns are tried; otherwise every 2 degrees. translation: fft over the whole footprint, no centroid
    assumption. walls_model: [(a, b)] wall segments of one floor."""
    from scipy.signal import fftconvolve
    P = _sample_segments(walls_model)
    if len(P) > 1500:
        P = P[np.linspace(0, len(P) - 1, 1500).astype(int)]
    pc = P.mean(0)
    P0 = P - pc
    rad = float(np.linalg.norm(P0, axis=1).max()) + 1.0
    F = np.asarray(footprint_enu, float)
    A, B = _segments(footprint_enu)
    lo = F.min(0) - rad - 3
    nx, ny = (((F.max(0) + rad + 3) - lo) / cell).astype(int) + 1
    gx, gy = lo[0] + cell * np.arange(nx), lo[1] + cell * np.arange(ny)
    G = np.stack(np.meshgrid(gx, gy), -1).reshape(-1, 2)
    sd = np.where(_inside(G, F), -1, 1) * _seg_dist(G, A, B)
    # +1 on the footprint edge, fading to 0 1.5 m off it; walls more than half a metre outside cost up to -5
    score = (np.clip(1 - np.abs(sd) / 1.5, 0, 1) - 5 * np.clip((sd - 0.5) / 1.5, 0, 1)).reshape(ny, nx)
    k = 2 * int(math.ceil(rad / cell)) + 1

    def best_at(deg):
        R = _rot(math.radians(deg))
        Q = P0 @ R.T
        ix = np.clip(np.round(Q[:, 0] / cell).astype(int) + k // 2, 0, k - 1)
        iy = np.clip(np.round(Q[:, 1] / cell).astype(int) + k // 2, 0, k - 1)
        K = np.zeros((k, k))
        np.add.at(K, (iy, ix), 1.0)
        S = fftconvolve(score, K[::-1, ::-1], mode="valid") / len(P0)
        j = np.unravel_index(S.argmax(), S.shape)
        c = np.array([gx[j[1] + k // 2], gy[j[0] + k // 2]])
        return float(S[j]), deg % 360, c - pc @ R.T

    hs, hf = _axis_hist(walls_model), _axis_hist(list(zip(footprint_enu, footprint_enu[1:])))
    if _peak_share(hs) > 0.5 and _peak_share(hf) > 0.5:
        off = int(np.argmax([np.dot(hs, np.roll(hf, -s_)) for s_ in range(90)]))
        bases = [off + 90 * q for q in range(4)]
        fine = np.arange(-2.0, 2.01, 0.5)
    else:
        bases = list(range(0, 360, 2))
        fine = np.array([0.0])
    per = sorted((max((best_at(b + dd) for dd in fine), key=lambda r: r[0]) for b in bases), key=lambda r: -r[0])
    picks = []
    for r in per:
        if all(abs((r[1] - p[1] + 180) % 360 - 180) > 20 for p in picks):
            picks.append(r)
        if len(picks) == 4:
            break
    if len(fine) == 1:  # full search: refine the picks in half degrees
        picks = sorted((max((best_at(p[1] + dd) for dd in np.arange(-2.0, 2.01, 0.5)), key=lambda r: r[0])
                        for p in picks), key=lambda r: -r[0])

    def metrics(deg, t):
        X = P @ _rot(math.radians(deg)).T + t
        d = _seg_dist(X, A, B)
        edge = d < 1.5
        rmse = float(np.sqrt((d[edge] ** 2).mean())) if edge.any() else float("inf")
        outside = float((~_inside(X, F) & (d > 1.0)).mean())
        # share of the footprint's perimeter that some scan wall lies along
        Qs = sample_ring(footprint_enu, 1.0)
        Xe = X[edge]
        cover = float((np.linalg.norm(Qs[:, None] - Xe[None], axis=2).min(1) < 1.0).mean()) if len(Xe) else 0.0
        return rmse, cover, outside

    s0, deg0, t0 = picks[0]
    rmse, cover, outside = metrics(deg0, t0)
    alts = []
    for s, deg, t in picks[1:]:
        r_, c_, o_ = metrics(deg, t)
        alts.append({"theta_deg": round(float(deg), 1), "tx": round(float(t[0]), 3), "ty": round(float(t[1]), 3),
                     "rmse_m": round(r_, 3), "inlier_frac": round(c_, 3), "score": round(s, 3),
                     "outside_frac": round(o_, 3)})
    alts = [a for a in alts if a["score"] > 0.6 * s0 and a["outside_frac"] <= 0.05]
    ambiguous = any(a["score"] > 0.85 * s0 for a in alts)
    # a partial scan touches only part of the outline, so coverage is low by nature: demand a tight edge fit
    # along at least 20 m of the outline, and walls inside the footprint
    perim = sum(math.dist(a, b) for a, b in zip(footprint_enu, list(footprint_enu[1:]) + [footprint_enu[0]]))
    ok = rmse <= OK_RMSE_M and cover * perim >= 20.0 and outside <= 0.03
    geo = Georef(enu, float(deg0), float(t0[0]), float(t0[1]), rmse, cover, ok, False, ambiguous, alts,
                 source="footprint-partial")
    geo.score, geo.outside_frac = round(s0, 3), round(outside, 3)
    return geo


def georef_model(g, lat, lon, footprint_lonlat, floor=None):
    """the whole thing: enu at the model's geolocation, footprint into enu, register the scan outline. when the
    hull doesn't fit (a partial scan of a bigger building), register the walls instead."""
    enu = ENU(lat, lon)
    fp = [enu.to_enu(x, y) for x, y in footprint_lonlat]
    geo = register(outline(g, floor), fp, enu=enu)
    if geo.ok:
        return geo
    try:
        part = register_partial(floor_walls(g, floor), fp, enu=enu)
    except Exception:  # no scipy, no walls
        return geo
    return part if part.ok else geo


def drone_anchor(sid, max_photos=60):
    """mean gps of a splat model's drone photos (flown around the building): a translation prior when the
    footprint is ambiguous. only the 232 splat models have photos. returns (lat, lon) or None."""
    import re
    import requests
    site = "https://beta-app.matterport.com/2026-princeton-hackathon"
    try:
        cat = requests.get(f"{site}/api/models/{sid}", timeout=60).json()
    except Exception:
        return None
    photos = (cat.get("images") or [])[:: max(1, len(cat.get("images") or []) // max_photos)]
    lats, lons = [], []
    for p in photos:
        head = requests.get(f"{site}/resources/{sid}/drone-imagery/{p}", headers={"Range": "bytes=0-49151"}, timeout=60).content
        tags = dict(re.findall(rb'drone-dji:(\w+)="([^"]*)"', head))
        if b"GpsLatitude" in tags and b"GpsLongitude" in tags:
            lats.append(float(tags[b"GpsLatitude"]))
            lons.append(float(tags[b"GpsLongitude"]))
    return (sum(lats) / len(lats), sum(lons) / len(lons)) if lats else None
