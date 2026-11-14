"""open-data fallbacks for the outdoor leg where vexcel has no coverage (hackathon keys: houston and la only).

  footprint:  openstreetmap building polygons via overpass (same mirrors, cache and user-agent as outdoor.py)
  elevation:  usgs 3dep (the data behind the elevation point query service), us only; sampled on a grid in
              batches of 100 with the 3dep imageserver's getSamples, epqs one point at a time as the fallback
  entrances:  osm entrance=* nodes (already in outdoor.overpass) plus the scan's entry-named places

works anywhere osm maps buildings and footways; elevation only in the us. no oblique imagery, so entrances
can't be confirmed visually here.
"""
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import outdoor, vexcel
from .mp import M_PER_DEG, ROOT

USGS = "https://epqs.nationalmap.gov/v1/json"
USGS_BATCH = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/getSamples"
USGS_CACHE = ROOT / "cache" / "usgs"
GRID_M = 4.0          # finest elevation sample spacing; doubled until the graph needs at most MAX_GRID samples
MAX_GRID = 3000       # cap on usgs samples per graph (cached, so it's paid once)
BATCH = 100           # points per getSamples request (longer urls get refused)
WORKERS = 2           # polite concurrency: each request takes ~15 s, and more at once draws 502s
USGS_TRANSPORT = None  # tests swap in a fake: fn(lon, lat) -> metres or None
FORCE = False          # open data even inside vexcel coverage (python -m nudge.opendata SID --open)

# hackathon vexcel coverage (anything outside answers 403 geofencing)
VEXCEL_BOXES = {"houston": (29.4, 30.3, -96.0, -94.9), "los angeles": (33.6, 34.5, -118.8, -117.6)}


def in_vexcel_coverage(lat, lon):
    return any(s <= lat <= n and w <= lon <= e for s, n, w, e in VEXCEL_BOXES.values())


# footprint

def _ring_contains(ring, lon, lat):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _ring_dist_m(ring, lon, lat):
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    return math.hypot((cx - lon) * M_PER_DEG * math.cos(math.radians(lat)), (cy - lat) * M_PER_DEG)


def _ring_area_m2(ring, lat):
    k = M_PER_DEG * math.cos(math.radians(lat))
    pts = [(x * k, y * M_PER_DEG) for x, y in ring]
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts))) / 2)


def buildings(lat, lon, radius_m=80):
    """osm building outlines near a point, each as {ring: [(lon, lat)], tags}."""
    q = f"""[out:json][timeout:25];
(
  way(around:{radius_m},{lat},{lon})[building];
  relation(around:{radius_m},{lat},{lon})[building];
);
out body geom;"""
    data = outdoor._overpass_query(q)
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        if el.get("type") == "way" and el.get("geometry"):
            ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
        elif el.get("type") == "relation":
            # multipolygon: the largest outer member
            outers = [[(p["lon"], p["lat"]) for p in m.get("geometry") or []]
                      for m in el.get("members") or [] if m.get("role") == "outer" and m.get("geometry")]
            if not outers:
                continue
            ring = max(outers, key=lambda r: _ring_area_m2(r, lat))
        else:
            continue
        if len(ring) >= 4:
            out.append({"ring": ring, "tags": tags, "osm": f"{el['type']}/{el['id']}"})
    return out


def footprint(lat, lon, radius_m=80):
    """the osm building at (lat, lon): the one containing the point, else the nearest. [(lon, lat)] closed ring,
    the same shape vexcel.footprint returns."""
    bs = buildings(lat, lon, radius_m)
    if not bs:
        raise LookupError(f"no osm building within {radius_m} m of {lat}, {lon}")
    containing = [b for b in bs if _ring_contains(b["ring"], lon, lat)]
    best = min(containing, key=lambda b: _ring_area_m2(b["ring"], lat)) if containing else \
        min(bs, key=lambda b: _ring_dist_m(b["ring"], lon, lat))
    ring = list(best["ring"])
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    footprint.last = best  # name / osm id for reports
    return ring


# elevation

def _metres(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v < -1000 else v  # usgs answers -1000000 outside coverage


def _usgs_point(lon, lat):
    if USGS_TRANSPORT is not None:
        return USGS_TRANSPORT(lon, lat)
    r = requests.get(USGS, params={"x": lon, "y": lat, "wkid": 4326, "units": "Meters", "includeDate": "false"},
                     headers={"User-Agent": outdoor.USER_AGENT}, timeout=60)
    r.raise_for_status()
    return _metres(r.json().get("value"))


def _usgs_batch(lonlats, tries=3):
    """elevations (m) for up to BATCH points in one request. retried, then halved; epqs point by point only for
    a handful of points (it takes ~13 s each)."""
    if USGS_TRANSPORT is None:
        geom = json.dumps({"points": [[round(x, 7), round(y, 7)] for x, y in lonlats],
                           "spatialReference": {"wkid": 4326}}, separators=(",", ":"))
        for k in range(tries):
            try:
                r = requests.get(USGS_BATCH, params={"geometry": geom, "geometryType": "esriGeometryMultipoint",
                                                     "returnFirstValueOnly": "true", "f": "json"},
                                 headers={"User-Agent": outdoor.USER_AGENT}, timeout=120)
                r.raise_for_status()
                out = [None] * len(lonlats)
                for smp in r.json()["samples"]:
                    out[smp["locationId"]] = _metres(smp.get("value"))
                return out
            except Exception:
                time.sleep(2 * (k + 1))
        if len(lonlats) > 4:
            h = len(lonlats) // 2
            return _usgs_batch(lonlats[:h], 2) + _usgs_batch(lonlats[h:], 2)
    return [USGSElevation._safe(x, y) for x, y in lonlats]


class USGSElevation:
    """elevation(points) for outdoor.build: samples usgs on a GRID_M grid around the points and interpolates
    bilinearly. raises if most of the grid has no data (outside the us), so build() drops slopes cleanly."""

    def __init__(self, lat, lon, grid_m=GRID_M):
        self.lat0, self.lon0 = lat, lon
        self.kx = M_PER_DEG * math.cos(math.radians(lat))
        self._regrid(grid_m)

    def _regrid(self, grid_m):
        self.grid = grid_m
        key = hashlib.sha1(f"{self.lat0:.5f},{self.lon0:.5f},{grid_m}".encode()).hexdigest()[:16]
        self.path = USGS_CACHE / f"{key}.json"
        try:
            self.z = {tuple(map(int, k.split(","))): v for k, v in json.loads(self.path.read_text()).items()}
        except (OSError, ValueError):
            self.z = {}

    def _cell(self, lon, lat):
        return ((lon - self.lon0) * self.kx / self.grid, (lat - self.lat0) * M_PER_DEG / self.grid)

    def _lonlat(self, i, j):
        return self.lon0 + i * self.grid / self.kx, self.lat0 + j * self.grid / M_PER_DEG

    def _fill(self, cells):
        todo = sorted(c for c in cells if c not in self.z)
        if len(todo) > MAX_GRID:
            raise RuntimeError(f"{len(todo)} usgs samples needed at {self.grid:.0f} m (cap {MAX_GRID})")
        if todo:
            chunks = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
            USGS_CACHE.mkdir(parents=True, exist_ok=True)
            with ThreadPoolExecutor(WORKERS) as pool:
                # saved as each batch lands, so an interrupted build keeps what it paid for
                for ch, vs in zip(chunks, pool.map(lambda ch: _usgs_batch([self._lonlat(*c) for c in ch]), chunks)):
                    self.z.update(zip(ch, vs))
                    self.path.write_text(json.dumps({f"{i},{j}": v for (i, j), v in self.z.items()}))

    @staticmethod
    def _safe(lon, lat):
        try:
            return _usgs_point(lon, lat)
        except Exception:
            return None

    def _cells(self, points):
        cells, fx = set(), []
        for lon, lat in points:
            u, v = self._cell(lon, lat)
            i, j = math.floor(u), math.floor(v)
            cells.update({(i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1)})
            fx.append((u, v, i, j))
        return cells, fx

    def __call__(self, points):
        cells, fx = self._cells(points)
        # a big graph: coarser grid (slopes then average over longer stretches) rather than hours of queries
        while sum(c not in self.z for c in cells) > MAX_GRID and self.grid < 32:
            self._regrid(self.grid * 2)
            cells, fx = self._cells(points)
        self._fill(cells)
        known = [self.z[c] for c in cells if self.z.get(c) is not None]
        if len(known) < 0.5 * len(cells):
            raise RuntimeError("usgs has no elevation here (outside the us?)")
        fallback = sum(known) / len(known)
        out = []
        for u, v, i, j in fx:
            q = [self.z.get(c) for c in ((i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1))]
            q = [fallback if z is None else z for z in q]
            a, b = u - i, v - j
            out.append(q[0] * (1 - a) * (1 - b) + q[1] * a * (1 - b) + q[2] * (1 - a) * b + q[3] * a * b)
        return out


# a plain-canvas check of the fit (no aerial image outside vexcel coverage)

def overlay_png(site, path, px=1000, half_m=None):
    """footprint (white), registered scan walls (yellow) and hull (dim yellow), walkable osm edges (grey; crossings
    orange), streets (dark grey), entrance candidates (cyan, best filled). north up, 10 m scale bar."""
    from PIL import Image, ImageDraw
    from .georef import floor_walls, outline
    from .handoff import georef_floor
    og, geo, gi = site.og, site.geo, site.gi
    fl = georef_floor(gi)
    ring_scan = [geo.model_to_enu(*p) for p in outline(gi, fl)]
    fp = og.footprint or []
    pts = fp + ring_scan
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    half = half_m or max(40.0, 1.6 * max(max(abs(p[0] - cx), abs(p[1] - cy)) for p in pts))
    s = px / (2 * half)

    def P(p):
        return ((p[0] - cx) * s + px / 2, px / 2 - (p[1] - cy) * s)

    im = Image.new("RGB", (px, px), (12, 12, 14))
    d = ImageDraw.Draw(im)
    for r in getattr(og, "roads", None) or []:
        d.line([P(p) for p in r["pts"]], fill=(70, 70, 76), width=6)
    for e in og.edges():
        a, b = og.nodes[e.a], og.nodes[e.b]
        col = (230, 120, 60) if e.tags & {"crossing", "unsignalised"} else (150, 150, 158)
        d.line([P(a.xy), P(b.xy)], fill=col, width=2)
    if fp:
        d.line([P(p) for p in fp + fp[:1]], fill=(240, 240, 240), width=3)
    d.line([P(p) for p in ring_scan + ring_scan[:1]], fill=(120, 100, 30), width=1)
    for a, b in floor_walls(gi, fl):
        d.line([P(geo.model_to_enu(*a)), P(geo.model_to_enu(*b))], fill=(250, 210, 40), width=2)
    for i, p in enumerate(site.portals):
        x, y = P(p["door_enu"])
        r = 9 if i == 0 else 6
        d.ellipse([x - r, y - r, x + r, y + r], outline=(60, 220, 240), width=3, fill=(60, 220, 240) if i == 0 else None)
    # north arrow and scale bar
    d.polygon([(px - 40, 20), (px - 50, 50), (px - 30, 50)], fill=(240, 240, 240))
    d.text((px - 44, 54), "N", fill=(240, 240, 240))
    d.line([(20, px - 30), (20 + 10 * s, px - 30)], fill=(240, 240, 240), width=4)
    d.text((20, px - 50), "10 m", fill=(240, 240, 240))
    d.text((20, 16), f"{site.sid}  source {site.source}  rot {geo.theta_deg:.1f}  rmse {geo.rmse_m:.2f} m",
           fill=(200, 200, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


# choosing a source

def footprint_any(lat, lon):
    """(ring, source, note). vexcel inside its coverage, osm otherwise or when vexcel refuses."""
    if in_vexcel_coverage(lat, lon) and not FORCE:
        try:
            return vexcel.footprint(lat, lon), "vexcel", None
        except Exception as ex:  # 401/403/no structure: fall through
            note = f"vexcel footprint unavailable ({ex}); using openstreetmap"
    else:
        note = "outside vexcel coverage; footprint from openstreetmap"
    ring = footprint(lat, lon)
    return ring, "osm", note


def elevation_for(lat, lon):
    """elevation function for outdoor.build: vexcel dtm inside its coverage, usgs elsewhere."""
    if in_vexcel_coverage(lat, lon) and not FORCE:
        return vexcel.elevation, "vexcel"
    return USGSElevation(lat, lon), "usgs"


def main(argv=None):
    """python -m nudge.opendata SID [--open] [--refresh]: the outdoor site for any scan with a geolocation, with
    no vexcel outside houston/la (--open forces open data inside them too). prints the fit and entrances and
    writes cache/bringup/<sid>_georef_osm.png."""
    import argparse
    from . import handoff
    ap = argparse.ArgumentParser(description="outdoor site from openstreetmap + usgs")
    ap.add_argument("sid")
    ap.add_argument("--open", action="store_true", help="use open data even inside vexcel coverage")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    global FORCE
    FORCE = a.open
    site = handoff.load_site(a.sid, refresh=a.refresh or a.open)
    g = site.geo
    print(f"{a.sid}: source {site.source}; scan on map: rotation {g.theta_deg:.1f}°, rmse {g.rmse_m:.2f} m, "
          f"{'ok' if g.ok else 'poor fit'}{', ambiguous' if g.ambiguous else ''} ({g.source})")
    print(f"outdoor graph {len(site.og.nodes)} nodes; {len(site.portals)} entrance candidate(s)")
    for p in site.portals[:5]:
        print(f"  {p['room']} ({p['source']}), faces {p['faces']}, snapped {p['snap_m']} m, "
              f"evidence: {(p.get('evidence') or {}).get('basis') or 'none'}")
    for w in site.warnings:
        print("  warning:", w)
    print("overlay:", overlay_png(site, ROOT / "cache" / "bringup" / f"{a.sid}_georef_osm.png"))


if __name__ == "__main__":
    main()
