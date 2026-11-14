"""what the dossier agent can see: pano views, cane-height hazards from the point cloud, glass, aerials.

all directions are in the model frame: metres, z up, yaw 0 = +x, ccw positive (nudge.geom.heading).
sid "synthetic" never touches the network; it renders from tests/synthetic.py and its point cloud.
"""
import io
import json
import math
import zipfile

import numpy as np
import requests
from PIL import Image, ImageDraw

from . import walkgraph
from .geom import dist
from .mp import ROOT, MatterportError, graph, vexcel_ortho

CACHE = ROOT / "cache"
FACE1_YAW_DEG = 0.0      # model yaw that skybox face 1 looks along. unverified until keys: see calibrate()
CLOUD_STRIDE = 10        # keep every nth point of cloud.xyz
GRID = 0.1               # metres per cell for the hazard grid
BAND = (0.685, 2.03)     # ada 307.2: 27-80 in above the floor
LOW = (0.05, 0.685)      # where a cane sweeps
NEAR_PATH = 1.0          # only care about things within this of a walk edge
MIN_PTS = 3              # points per cell to count as occupied
MIN_CELLS = 2            # smallest cluster worth reporting
HEADER_Z = 1.98          # a cluster whose underside is above this is a door header, not a hazard
GLASS_BAND = (0.5, 2.0)
GLASS_RATIO = 0.35       # wall support below this fraction of the floor median is suspect

_graphs = {}


class PerceiveError(RuntimeError):
    pass


# model + graph

def _model(sid):
    from .fetch import load
    return load(sid)


def _graph(sid):
    if sid not in _graphs:
        _graphs[sid] = walkgraph.build(_model(sid))
    return _graphs[sid]


def _node(g, pano):
    if pano in g.nodes:
        return g.nodes[pano]
    hit = next((n for n in g.nodes.values() if n.label == str(pano)), None)
    if not hit:
        raise PerceiveError(f"no pano {pano!r}")
    return hit


# skybox -> perspective

def rays(yaw_deg, pitch_deg, fov_deg, w, h):
    """unit view rays (h, w, 3) in the model frame. fov is horizontal."""
    psi, p = math.radians(yaw_deg), math.radians(pitch_deg)
    f = np.array([math.cos(psi) * math.cos(p), math.sin(psi) * math.cos(p), math.sin(p)])
    r = np.array([math.sin(psi), -math.cos(psi), 0.0])   # right of the view, always level
    u = np.cross(r, f)
    t = math.tan(math.radians(fov_deg) / 2)
    xs = ((np.arange(w) + 0.5) / w * 2 - 1) * t
    ys = ((np.arange(h) + 0.5) / h * 2 - 1) * t * h / w
    X, Y = np.meshgrid(xs, ys)
    d = f + X[..., None] * r - Y[..., None] * u
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def _to_camera(d, face1_yaw_deg):
    """model frame -> skybox camera frame (x right, y up, z = face 1)."""
    a = -math.radians(face1_yaw_deg)
    rx = math.cos(a) * d[..., 0] - math.sin(a) * d[..., 1]
    ry = math.sin(a) * d[..., 0] + math.cos(a) * d[..., 1]
    return np.stack([-ry, d[..., 2], rx], -1)


def sample_cube(faces, cam):
    """faces: six (n, n, 3) arrays, 0 up, 1-4 around the horizon turning right, 5 down."""
    x, y, z = cam[..., 0], cam[..., 1], cam[..., 2]
    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    out = np.zeros(cam.shape[:-1] + (3,), np.uint8)
    n = faces[0].shape[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        for k, mask, u, v in [
            (1, (az >= ax) & (az >= ay) & (z > 0), x / az, -y / az),
            (2, (ax >= ay) & (ax >= az) & (x > 0), -z / ax, -y / ax),
            (3, (az >= ax) & (az >= ay) & (z < 0), -x / az, -y / az),
            (4, (ax >= ay) & (ax >= az) & (x < 0), z / ax, -y / ax),
            (0, (ay >= ax) & (ay >= az) & (y > 0), x / ay, z / ay),
            (5, (ay >= ax) & (ay >= az) & (y < 0), x / ay, -z / ay),
        ]:
            col = np.clip(((u[mask] + 1) / 2 * n).astype(int), 0, n - 1)
            row = np.clip(((v[mask] + 1) / 2 * n).astype(int), 0, n - 1)
            out[mask] = faces[k][row, col]
    return out


def render_skybox(faces, yaw_deg, pitch_deg=0.0, fov_deg=90.0, size=768, face1_yaw_deg=None):
    off = FACE1_YAW_DEG if face1_yaw_deg is None else face1_yaw_deg
    return sample_cube(faces, _to_camera(rays(yaw_deg, pitch_deg, fov_deg, size, size), off))


Q_SKYBOX = """query($id: ID!) { model(id: $id) { panoLocations { id skybox(resolution: "2k") { status children } } } }"""
# guesses at a per-pano rotation field; the first one the schema accepts wins
ROTATION_FIELDS = ["rotation { x y z w }", "orientation { x y z w }"]


def _faces(sid, pano_id):
    d = CACHE / "panos" / sid / pano_id
    paths = [d / f"face{i}.jpg" for i in range(6)]
    if not all(p.exists() for p in paths):
        locs = graph(Q_SKYBOX, id=sid)["model"]["panoLocations"]
        loc = next((l for l in locs if l["id"] == pano_id), None)
        urls = ((loc or {}).get("skybox") or {}).get("children") or []
        if len(urls) != 6:
            raise PerceiveError(f"no skybox for pano {pano_id}")
        d.mkdir(parents=True, exist_ok=True)
        for p, u in zip(paths, urls):  # signed urls expire: fetch now, never store them
            r = requests.get(u, timeout=120)
            r.raise_for_status()
            p.write_bytes(r.content)
    return [np.asarray(Image.open(p).convert("RGB")) for p in paths]


def pano_rotation_deg(sid, pano_id):
    """yaw (deg, about z) of a per-pano rotation if the api exposes one, else 0. unverified."""
    if sid == "synthetic":
        return 0.0
    f = CACHE / "panos" / sid / "rotation.json"
    rot = json.loads(f.read_text()) if f.exists() else None
    if rot is None:
        rot = {}
        for field in ROTATION_FIELDS:
            try:
                locs = graph(f"query($id: ID!) {{ model(id: $id) {{ panoLocations {{ id {field} }} }} }}",
                             id=sid)["model"]["panoLocations"]
            except MatterportError:
                continue
            for l in locs:
                q = l.get(field.split()[0])
                if q:
                    x, y, z, w = (float(q.get(k) or 0) for k in "xyzw")
                    rot[l["id"]] = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
            break
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rot))
    return float(rot.get(pano_id, 0.0))


# synthetic view: ray-cast the wall graph

def _wall_hits(g, floor, ox, oy, dx, dy):
    """distance along unit horizontal dirs (dx, dy arrays) to the first blocking wall; inf if none."""
    best = np.full(dx.shape, np.inf)
    for a, b, t in g.walls(floor):
        if t in walkgraph.PASSABLE:
            continue
        ex, ey = b[0] - a[0], b[1] - a[1]
        wx, wy = a[0] - ox, a[1] - oy
        den = dx * ey - dy * ex
        with np.errstate(divide="ignore", invalid="ignore"):
            tt = (wx * ey - wy * ex) / den
            ss = (wx * dy - wy * dx) / den
        ok = (np.abs(den) > 1e-12) & (tt > 1e-6) & (ss >= 0) & (ss <= 1)
        best = np.where(ok & (tt < best), tt, best)
    return best


def wall_distance(g, node, yaw_deg):
    a = math.radians(yaw_deg)
    d = _wall_hits(g, node.floor, node.x, node.y, np.array([math.cos(a)]), np.array([math.sin(a)]))[0]
    return None if not np.isfinite(d) else round(float(d), 2)


def _render_synthetic(g, node, yaw_deg, pitch_deg, fov_deg, size, cam_h=1.5, ceil_h=2.7):
    d = rays(yaw_deg, pitch_deg, fov_deg, size, size)
    hxy = np.hypot(d[..., 0], d[..., 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        dx, dy = d[..., 0] / hxy, d[..., 1] / hxy
    D = _wall_hits(g, node.floor, node.x, node.y, np.nan_to_num(dx), np.nan_to_num(dy))
    with np.errstate(divide="ignore", invalid="ignore"):
        zhit = cam_h + D * d[..., 2] / hxy
    out = np.zeros((size, size, 3), np.uint8)
    wall = np.isfinite(D) & (zhit >= 0) & (zhit <= ceil_h)
    shade = np.clip(215 - 14 * np.nan_to_num(D, posinf=20), 70, 215).astype(np.uint8)
    out[wall] = np.stack([shade[wall]] * 3, -1)
    floor = ~wall & (d[..., 2] < 0)
    ceil = ~wall & ~floor
    out[floor] = (150, 120, 90)
    out[ceil] = (235, 235, 240)
    return out


# overlay

def _overlay(img, yaw_deg, pitch_deg, lines):
    dr = ImageDraw.Draw(img)
    cx, cy, r = 40, 40, 28
    dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0), outline=(255, 255, 255))
    dr.line([cx + r - 6, cy, cx + r, cy], fill=(255, 80, 80), width=2)   # +x tick
    dr.text((cx + r + 3, cy - 6), "+x", fill=(255, 80, 80))
    a = math.radians(yaw_deg)
    dr.line([cx, cy, cx + (r - 4) * math.cos(a), cy - (r - 4) * math.sin(a)], fill=(255, 220, 0), width=3)
    text = [f"yaw {yaw_deg:.0f} pitch {pitch_deg:.0f} (model)"] + lines
    for i, t in enumerate(text):
        dr.rectangle([4, 74 + 14 * i, 8 + 6 * len(t), 88 + 14 * i], fill=(0, 0, 0))
        dr.text((6, 75 + 14 * i), t, fill=(255, 255, 255))


def _jpeg(img, q=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def look(sid, pano_id, yaw_deg, pitch_deg=0.0, fov_deg=90.0, size=768):
    """perspective view from a pano. returns (jpeg_bytes, meta)."""
    g = _graph(sid)
    n = _node(g, pano_id)
    meta = {"sid": sid, "pano_id": n.id, "yaw_deg": float(yaw_deg), "pitch_deg": float(pitch_deg),
            "fov_deg": float(fov_deg), "xy": [round(n.x, 3), round(n.y, 3)], "floor": n.floor,
            "room_label": n.room_label}
    if sid == "synthetic":
        arr = _render_synthetic(g, n, yaw_deg, pitch_deg, fov_deg, size)
        lines = [f"SYNTHETIC {n.label} {n.room_label or ''}"]
    else:
        off = FACE1_YAW_DEG + pano_rotation_deg(sid, n.id)
        arr = render_skybox(_faces(sid, n.id), yaw_deg, pitch_deg, fov_deg, size, off)
        lines = [f"pano {n.label} {n.room_label or ''}"]
    img = Image.fromarray(arr)
    _overlay(img, yaw_deg, pitch_deg, lines)
    return _jpeg(img), meta


def calibrate(sid, pano_id):
    """check which way skybox face 1 points in the model frame.

    renders the four horizon views at model yaw 0/90/180/270 and gives, for each, the distance to the
    nearest wall along that bearing from the wall graph. at the right FACE1_YAW_DEG the view whose wall
    looks nearest is the one with the smallest expected_wall_m. if instead every view looks like the
    expectation one step clockwise (or ccw), set FACE1_YAW_DEG -= 90 (or += 90) and run again; panos in
    long corridors make this obvious. if different panos disagree, there is a per-pano rotation
    (pano_rotation_deg) and we need the right field from the api.
    """
    g = _graph(sid)
    n = _node(g, pano_id)
    out = []
    for yaw in (0, 90, 180, 270):
        jpg, _ = look(sid, n.id, yaw, 0.0, 90.0, 512)
        out.append({"yaw_deg": yaw, "jpeg": jpg, "expected_wall_m": wall_distance(g, n, yaw)})
    return {"pano_id": n.id, "face1_yaw_deg": FACE1_YAW_DEG,
            "rotation_deg": pano_rotation_deg(sid, n.id), "views": out}


# point cloud

Q_MATTERPAK = """query($id: ID!) { model(id: $id) {
  bundle(id: "mp:matterpak") { availability assets { ... on Resource { format status filename url } } } } }"""


class _HttpFile(io.RawIOBase):
    """seekable read-only file over http range requests, so zipfile can read a remote zip in place."""

    def __init__(self, url, block=8 * 2**20):
        self.url, self.pos, self.block, self.buf, self.at = url, 0, block, b"", -1
        r = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=60)
        r.raise_for_status()
        self.size = int(r.headers["Content-Range"].split("/")[1])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        if not (self.at <= self.pos and self.pos + n <= self.at + len(self.buf)):
            end = min(self.size, self.pos + max(n, self.block)) - 1
            r = requests.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=300)
            r.raise_for_status()
            self.at, self.buf = self.pos, r.content
        out = self.buf[self.pos - self.at:self.pos - self.at + n]
        self.pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def _parse_xyz(lines):
    lines = [l for l in lines if l.strip()]
    if not lines:
        return np.zeros((0, 3), np.float32)
    k = len(lines[0].split())
    lines = [l for l in lines if len(l.split()) == k]
    return np.array(b" ".join(lines).split(), dtype=np.float32).reshape(-1, k)[:, :3]


def cloud(sid, stride=CLOUD_STRIDE):
    """(n, 3) float32 xyz in the model frame, every `stride`th point, cached."""
    if sid == "synthetic":
        from tests.synthetic_perceive import cloud as syn
        return syn()
    path = CACHE / "clouds" / f"{sid}_s{stride}.npy"
    if path.exists():
        return np.load(path)
    b = graph(Q_MATTERPAK, id=sid)["model"].get("bundle") or {}
    pak = next((a for a in b.get("assets") or [] if a.get("url")), None)
    if not pak:
        raise PerceiveError(f"matterpak for {sid} is {b.get('availability', 'unavailable')}")
    z = zipfile.ZipFile(_HttpFile(pak["url"]))
    name = next((n for n in z.namelist() if n.endswith("cloud.xyz")), None)
    if not name:
        raise PerceiveError(f"no cloud.xyz in the matterpak for {sid}")
    parts, seen, rest = [], 0, b""
    with z.open(name) as f:
        while True:
            chunk = f.read(32 * 2**20)
            if not chunk:
                break
            buf = rest + chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                rest = buf
                continue
            lines, rest = buf[:cut].split(b"\n"), buf[cut + 1:]
            parts.append(_parse_xyz(lines[(-seen) % stride::stride]))
            seen += len(lines)
    if rest.strip() and seen % stride == 0:
        parts.append(_parse_xyz([rest]))
    pts = np.concatenate(parts) if parts else np.zeros((0, 3), np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, pts)
    return pts


def floor_levels(g, pts):
    """{floor_id: (floor_z, top_z)} from pano heights, refined by the densest z bin near pano_z - 1.5."""
    est = {}
    for f in g.floors:
        zs = [n.z for n in g.nodes.values() if n.floor == f["id"]]
        if not zs:
            continue
        guess = float(np.median(zs)) - 1.5
        win = pts[(pts[:, 2] > guess - 0.6) & (pts[:, 2] < guess + 0.6), 2]
        if len(win) > 50:
            hist, edges = np.histogram(win, bins=np.arange(guess - 0.6, guess + 0.62, 0.02))
            i = int(np.argmax(hist))
            guess = float((edges[i] + edges[i + 1]) / 2)
        est[f["id"]] = guess
    order = sorted(est, key=est.get)
    return {fid: (est[fid], est[order[i + 1]] - 0.1 if i + 1 < len(order) else est[fid] + 3.5)
            for i, fid in enumerate(order)}


def _floor_points(sid, floor_id):
    g = _graph(sid)
    pts = cloud(sid)
    lv = floor_levels(g, pts)
    if floor_id not in lv:
        raise PerceiveError(f"no panos on floor {floor_id}")
    z0, z1 = lv[floor_id]
    sel = pts[(pts[:, 2] >= z0 - 0.1) & (pts[:, 2] < z1)]
    return g, sel[:, :2], sel[:, 2] - z0


def _walk_segments(g, floor_id):
    return [(g.nodes[e.a].xy, g.nodes[e.b].xy, (e.a, e.b)) for e in g.edges()
            if g.nodes[e.a].floor == floor_id and g.nodes[e.b].floor == floor_id and "vertical" not in e.tags]


def _seg_dist(px, py, segs):
    """(len(p), len(segs)) distances from points to segments."""
    A = np.array([s[0] for s in segs])
    B = np.array([s[1] for s in segs])
    E = B - A
    L2 = np.maximum((E ** 2).sum(1), 1e-12)
    t = np.clip(((px[:, None] - A[:, 0]) * E[:, 0] + (py[:, None] - A[:, 1]) * E[:, 1]) / L2, 0, 1)
    cx, cy = A[:, 0] + t * E[:, 0], A[:, 1] + t * E[:, 1]
    return np.hypot(px[:, None] - cx, py[:, None] - cy)


def _viewpoint(g, floor_id, x, y):
    """nearest pano with a clear line to (x, y); falls back to the nearest one."""
    ns = sorted((n for n in g.nodes.values() if n.floor == floor_id), key=lambda n: dist(n.xy, (x, y)))
    if not ns:
        return None, None, None
    for n in ns:
        d = dist(n.xy, (x, y))
        if d < 1e-6:
            return n, 0.0, 0.0
        # stop just short of the target: things sit against walls
        k = max(0.0, (d - 0.15) / d)
        tgt = (n.x + k * (x - n.x), n.y + k * (y - n.y))
        if not g.blocked_line(n.xy, tgt, floor_id):
            break
    else:
        n = ns[0]
    return n, round(dist(n.xy, (x, y)), 2), round(math.degrees(math.atan2(y - n.y, x - n.x)), 1)


def _grid(xy, h, sel, x0, y0, nx, ny):
    m = sel(h)
    ix = ((xy[m, 0] - x0) / GRID).astype(int)
    iy = ((xy[m, 1] - y0) / GRID).astype(int)
    ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    return np.bincount(iy[ok] * nx + ix[ok], minlength=nx * ny).reshape(ny, nx)


def _dilate(mask):
    p = np.pad(mask, 1)
    out = np.zeros_like(mask)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out |= p[dy:dy + mask.shape[0], dx:dx + mask.shape[1]]
    return out


def _clusters(mask):
    seen = np.zeros_like(mask)
    out = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        stack, cells = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny_, nx_ = cy + dy, cx + dx
                    if 0 <= ny_ < mask.shape[0] and 0 <= nx_ < mask.shape[1] and mask[ny_, nx_] and not seen[ny_, nx_]:
                        seen[ny_, nx_] = True
                        stack.append((ny_, nx_))
        out.append(cells)
    return out


def overhang_candidates(sid, floor_id, max_items=25):
    """things in the 27-80 in band with nothing below for a cane to find, near where people walk."""
    g, xy, h = _floor_points(sid, floor_id)
    segs = _walk_segments(g, floor_id)
    if len(xy) == 0 or not segs:
        return []
    x0, y0 = xy[:, 0].min() - GRID, xy[:, 1].min() - GRID
    nx = int((xy[:, 0].max() - x0) / GRID) + 2
    ny = int((xy[:, 1].max() - y0) / GRID) + 2
    band = _grid(xy, h, lambda h: (h >= BAND[0]) & (h <= BAND[1]), x0, y0, nx, ny) >= MIN_PTS
    low = _grid(xy, h, lambda h: (h >= LOW[0]) & (h < LOW[1]), x0, y0, nx, ny) > 0
    cand = band & ~_dilate(low)   # nothing low within a cell of it either: a cane sweeps an arc
    iy, ix = np.nonzero(cand)
    if len(iy) == 0:
        return []
    cxs, cys = x0 + (ix + 0.5) * GRID, y0 + (iy + 0.5) * GRID
    near = _seg_dist(cxs, cys, [s[:2] for s in segs]).min(1) <= NEAR_PATH
    keep = np.zeros_like(cand)
    keep[iy[near], ix[near]] = True
    clusters = [c for c in _clusters(keep) if len(c) >= MIN_CELLS]
    labels = np.full(cand.shape, -1)
    for k, cells in enumerate(clusters):
        cy_, cx_ = np.array(cells).T
        labels[cy_, cx_] = k
    pix = ((xy[:, 0] - x0) / GRID).astype(int)
    piy = ((xy[:, 1] - y0) / GRID).astype(int)
    inb = (pix >= 0) & (pix < nx) & (piy >= 0) & (piy < ny)
    plab = np.full(len(xy), -1)
    plab[inb] = labels[piy[inb], pix[inb]]
    plab[~((h >= BAND[0]) & (h <= BAND[1]))] = -1
    out = []
    for k, cells in enumerate(clusters):
        cy_, cx_ = np.array(cells).T
        pm = plab == k
        if not pm.any():
            continue
        zl, zh = float(h[pm].min()), float(h[pm].max())
        if zl > HEADER_Z:
            continue
        x, y = float(x0 + (cx_.mean() + 0.5) * GRID), float(y0 + (cy_.mean() + 0.5) * GRID)
        ext = max(np.ptp(cx_), np.ptp(cy_)) * GRID + GRID
        dseg = _seg_dist(np.array([x]), np.array([y]), [s[:2] for s in segs])[0]
        n, d, brg = _viewpoint(g, floor_id, x, y)
        out.append({"x": round(x, 2), "y": round(y, 2), "floor": floor_id, "z_low_m": round(zl, 2),
                    "z_high_m": round(zh, 2), "size_m": round(float(ext), 2), "points": int(pm.sum()),
                    "nearest_pano": n.id if n else None, "distance_m": d, "bearing_deg": brg,
                    "on_edges": [list(segs[i][2]) for i in np.nonzero(dseg <= NEAR_PATH)[0]],
                    "_path_m": float(dseg.min())})
    out.sort(key=lambda c: (c["_path_m"], -c["points"]))
    for i, c in enumerate(out):
        c.pop("_path_m")
        c["id"] = f"oh-{floor_id}-{i}"
    return out[:max_items]


def glass_suspects(sid, floor_id, max_items=25):
    """walls the scanner barely saw. glass is near-invisible in photogrammetry, so these need a look."""
    g, xy, h = _floor_points(sid, floor_id)
    m = (h >= GLASS_BAND[0]) & (h < GLASS_BAND[1])
    pts = xy[m]
    if len(pts) == 0:
        return []
    cell = 0.05
    x0, y0 = pts[:, 0].min() - 1, pts[:, 1].min() - 1
    nx = int((pts[:, 0].max() - x0) / cell) + 2
    ny = int((pts[:, 1].max() - y0) / cell) + 2
    occ = np.bincount(((pts[:, 1] - y0) / cell).astype(int) * nx + ((pts[:, 0] - x0) / cell).astype(int),
                      minlength=nx * ny).reshape(ny, nx)
    seen, walls = set(), []
    for a, b, t in g.walls(floor_id):
        if t in walkgraph.PASSABLE or dist(a, b) < 0.5:
            continue
        k = tuple(sorted([(round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))]))
        if k in seen:
            continue
        seen.add(k)
        L = dist(a, b)
        ts = np.arange(0.15, L - 0.15 + 1e-9, 0.1) / L   # skip the corners, other walls live there
        if len(ts) == 0:
            continue
        sx, sy = a[0] + ts * (b[0] - a[0]), a[1] + ts * (b[1] - a[1])
        tot = 0
        for px, py in zip(sx, sy):
            cx, cy = int((px - x0) / cell), int((py - y0) / cell)
            tot += int(occ[max(0, cy - 2):cy + 3, max(0, cx - 2):cx + 3].sum())
        walls.append((a, b, tot / len(ts)))
    if not walls:
        return []
    med = float(np.median([w[2] for w in walls])) or 1.0
    out = []
    for a, b, s in sorted(walls, key=lambda w: w[2]):
        ratio = s / med
        if ratio >= GLASS_RATIO:
            break
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        n, d, brg = _viewpoint(g, floor_id, mx, my)
        out.append({"id": f"gl-{floor_id}-{len(out)}", "a": [round(a[0], 3), round(a[1], 3)],
                    "b": [round(b[0], 3), round(b[1], 3)], "floor": floor_id, "support_ratio": round(ratio, 3),
                    "nearest_pano": n.id if n else None, "bearing_deg": brg, "distance_m": d})
    return out[:max_items]


# aerial

def aerial(sid, half_m=60, px=1024):
    """vexcel ortho around the model, north up, with a north arrow and a scale bar. (jpeg_bytes, meta)"""
    if sid == "synthetic":
        lat, lon = 0.0, 0.0
        img = Image.new("RGB", (px, px), (120, 130, 120))
        g = _graph(sid)
        s = px / (2 * half_m)
        dr = ImageDraw.Draw(img)
        for a, b, _ in g.walls(g.floors[0]["id"]):   # floor 1 footprint, centred, as a stand-in roof
            dr.line([px / 2 + (a[0] - 9.5) * s, px / 2 - (a[1] - 6) * s,
                     px / 2 + (b[0] - 9.5) * s, px / 2 - (b[1] - 6) * s], fill=(230, 230, 230), width=3)
        dr.text((10, px - 20), "SYNTHETIC aerial placeholder", fill=(255, 255, 255))
    else:
        geo = _model(sid).get("geolocation") or {}
        if not geo.get("lat"):
            raise PerceiveError(f"{sid} has no lat/lon")
        lat, lon = float(geo["lat"]), float(geo["long"])
        img = Image.open(io.BytesIO(vexcel_ortho(lat, lon, half_m, px))).convert("RGB")
    gsd = 2 * half_m / img.width
    dr = ImageDraw.Draw(img)
    w, hgt = img.size
    dr.polygon([(w - 40, 20), (w - 50, 50), (w - 30, 50)], fill=(255, 255, 255), outline=(0, 0, 0))
    dr.text((w - 44, 54), "N", fill=(255, 255, 255))
    bar = 10 / gsd   # 10 m
    dr.rectangle([20, hgt - 40, 20 + bar, hgt - 32], fill=(255, 255, 255), outline=(0, 0, 0))
    dr.text((20, hgt - 56), "10 m", fill=(255, 255, 255))
    cx, cy = w / 2, hgt / 2
    dr.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(255, 60, 60), width=2)   # the model's lat/lon
    return _jpeg(img), {"lat": lat, "lon": lon, "half_m": half_m, "gsd_m": round(gsd, 4)}
