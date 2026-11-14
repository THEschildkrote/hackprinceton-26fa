"""put a matterport model's frame on the map.

the model api gives one lat/lon per model; the frame's rotation is unknown. we register the scan's
outer outline (hull of the ground floor's wall vertices) against vexcel's building footprint in local
east/north metres: grid search rotation, align centroids, refine with point-to-segment icp, score with a
truncated symmetric chamfer distance. symmetric shapes (rectangles) stay ambiguous; we say so.
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
                "source": self.source, "origin": [self.enu.lat0, self.enu.lon0] if self.enu else None}


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


def georef_model(g, lat, lon, footprint_lonlat, floor=None):
    """the whole thing: enu at the model's geolocation, footprint into enu, register the scan outline."""
    enu = ENU(lat, lon)
    fp = [enu.to_enu(x, y) for x, y in footprint_lonlat]
    return register(outline(g, floor), fp, enu=enu)


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
