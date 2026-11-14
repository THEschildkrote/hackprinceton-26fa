"""thin cached client for the vexcel api v2, written against reference/vexcel_api_v2.json.

auth: X-API-KEY header (VEXCEL_API_KEY). hackathon keys cover houston and los angeles only.
all coordinates in and out are lon/lat (EPSG:4326) unless a function says otherwise.
"""
import hashlib
import json
import math
import re

import requests

from .mp import M_PER_DEG, ROOT, secret

BASE = "https://api.vexcelgroup.com/v2"
CACHE = ROOT / "cache" / "vexcel"
LAYERS = "urban,urban-r,wide-area,urban-g"
DEM = "vexcel-dtm,public-dtm"
DIRECTIONS = ("north", "south", "east", "west")

# tests swap this for a fake: transport(method, path, params, body) -> (status, content_type, bytes)
TRANSPORT = None


class VexcelError(RuntimeError):
    pass


def _http(method, path, params, body):
    h = {"X-API-KEY": secret("VEXCEL_API_KEY")}
    url = f"{BASE}{path}"
    if method == "GET":
        r = requests.get(url, params=params, headers=h, timeout=120)
    else:
        r = requests.post(url, json=body, headers=h, timeout=300)
    return r.status_code, r.headers.get("content-type", ""), r.content


def _call(method, path, params=None, body=None, binary=False):
    key = hashlib.sha1(json.dumps([method, path, params, body], sort_keys=True).encode()).hexdigest()[:20]
    cached = CACHE / (key + (".bin" if binary else ".json"))
    if TRANSPORT is None and cached.exists():
        data = cached.read_bytes()
        return data if binary else json.loads(data)
    status, ctype, data = (TRANSPORT or _http)(method, path, params, body)
    if status == 204:
        return b"" if binary else {}
    if status == 401:
        raise VexcelError("vexcel rejected the key (401); check VEXCEL_API_KEY")
    if status == 403:
        raise VexcelError("vexcel answered 403; hackathon keys cover houston and los angeles only")
    if status >= 400:
        raise VexcelError(f"vexcel {path} answered {status}: {data[:200]!r}")
    if TRANSPORT is None:
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return data if binary else json.loads(data)


# wkt helpers (lon lat order)

def wkt_point(lon, lat):
    return f"POINT({lon} {lat})"


def wkt_line(pts):
    return "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in pts) + ")"


def wkt_square(lat, lon, half_m):
    dlat = half_m / M_PER_DEG
    dlon = half_m / (M_PER_DEG * math.cos(math.radians(lat)))
    w, e, s, n = lon - dlon, lon + dlon, lat - dlat, lat + dlat
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def parse_wkt_coords(wkt):
    """every coordinate tuple in a wkt string, as floats (2 or 3 per tuple)."""
    body = wkt[wkt.index("("):] if "(" in wkt else wkt
    out = []
    for chunk in re.findall(r"[-0-9.eE+]+(?:\s+[-0-9.eE+]+){1,3}", body):
        out.append(tuple(float(v) for v in chunk.split()))
    return out


def _outer_ring(geom):
    """outer ring of a geojson Polygon / MultiPolygon (largest part), as [(lon, lat)]."""
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon":
        return [tuple(p[:2]) for p in c[0]]
    if t == "MultiPolygon":
        rings = [[tuple(p[:2]) for p in poly[0]] for poly in c]
        return max(rings, key=lambda r: abs(_ring_area(r)))
    raise VexcelError(f"unexpected footprint geometry {t}")


def _ring_area(r):
    return sum(r[i][0] * r[i - 1][1] - r[i - 1][0] * r[i][1] for i in range(len(r))) / 2


def _features(resp):
    if isinstance(resp, dict) and resp.get("type") == "FeatureCollection":
        return resp.get("features") or []
    if isinstance(resp, dict) and resp.get("type") == "Feature":
        return [resp]
    if isinstance(resp, list):
        return resp
    return (resp or {}).get("features") or []


# endpoints

def footprint(lat, lon, buffer_m=15):
    """footprint of the main structure at a point: /structure/geometry/extract. returns [(lon, lat)] closed ring."""
    resp = _call("GET", "/structure/geometry/extract", {
        "wkt": wkt_point(lon, lat), "srid": "4326", "buffer-meter": buffer_m, "primary-structure": "true",
        "latest": "true", "total-records": 1, "unit-system": "metric"})
    feats = _features(resp)
    if not feats:
        raise VexcelError(f"no structure footprint at {lat}, {lon}")
    return _outer_ring(feats[0]["geometry"])


def footprints(lat, lon, half_m=60, max_records=50):
    """every footprint in a square around a point, for context and obstacle checks."""
    resp = _call("GET", "/structure/geometry/extract", {
        "wkt": wkt_square(lat, lon, half_m), "srid": "4326", "latest": "true", "total-records": max_records,
        "unit-system": "metric"})
    return [_outer_ring(f["geometry"]) for f in _features(resp) if f.get("geometry")]


def structure(lat, lon):
    """structure attributes (heights, elevations, area) at a point: /structure/extract."""
    resp = _call("GET", "/structure/extract", {"wkt": wkt_point(lon, lat), "srid": "4326", "primary-structure": "true",
                                                "total-records": 1, "unit-system": "metric"})
    feats = _features(resp)
    return (feats[0].get("properties") or {}) if feats else {}


def elevation(points, dem=DEM, chunk=100):
    """ground elevation in metres at each (lon, lat): /dtm/elevation with a LINESTRING (POINT for one)."""
    out = []
    for i in range(0, len(points), chunk):
        part = points[i:i + chunk]
        wkt = wkt_point(*part[0]) if len(part) == 1 else wkt_line(part)
        resp = _call("GET", "/dtm/elevation", {"wkt": wkt, "srid": "4326", "dem-priority": dem, "unit-system": "metric"})
        coords = parse_wkt_coords(resp.get("wkt", ""))
        if len(coords) != len(part) or any(len(c) < 3 for c in coords):
            raise VexcelError(f"/dtm/elevation returned {len(coords)} points for {len(part)}")
        out += [c[2] for c in coords]
    return out


def oblique_views(lat, lon, include="image-name,product-type,capture-date,camera-pos-x,camera-pos-y,camera-pos-z,image-center"):
    """oblique images covering a point: /oriented/query. returns [{image_name, direction, capture_date, camera}]."""
    resp = _call("GET", "/oriented/query", {"wkt": wkt_point(lon, lat), "srid": 4326, "layer": LAYERS,
                                             "product-type": "oblique", "include": include, "metadata-format": "json"})
    rows = [f.get("properties") or {} for f in _features(resp)] or (resp.get("fields") if isinstance(resp, dict) else []) or []
    out = []
    for r in rows:
        pt = str(r.get("product-type", ""))
        out.append({"image_name": r.get("image-name"), "direction": pt.replace("oblique-", "") or None,
                    "capture_date": r.get("capture-date"),
                    "camera": [r.get("camera-pos-x"), r.get("camera-pos-y"), r.get("camera-pos-z")]})
    return out


def oblique_image(lat, lon, direction="north", half_m=25, image_name=None, crosshair=True, px=1024):
    """one oblique facade view looking from `direction`: /oriented/extract. returns jpeg bytes."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    p = {"wkt": wkt_point(lon, lat), "srid": "4326", "layer": LAYERS, "product-type": f"oblique-{direction}",
         "bbox-dimensions": f"{2 * half_m},{2 * half_m}", "destination-dimensions": f"{px},{px}",
         "image-format": "jpeg", "attribution": "false", "rotation": "natural"}
    if image_name:
        p["image-name"] = image_name
    if crosshair:
        p["crosshair"] = wkt_point(lon, lat)
    return _call("GET", "/oriented/extract", p, binary=True)


def _transform(image_name, op, pts):
    wkt = wkt_point(*pts[0]) if len(pts) == 1 else wkt_line(pts)
    resp = _call("GET", "/oriented/transform-points", {"operation": op, "image-name": image_name, "wkt": wkt,
                                                        "srid": "4326", "metadata-format": "json", "dem-priority": DEM})
    return [(p["x"], p["y"]) for p in resp.get("points") or []]


def pixel_to_world(image_name, pixels):
    """[(px, py)] in an oriented image -> [(lon, lat)], using vexcel's photogrammetry (not the model's guess)."""
    return _transform(image_name, "pixel-2-world", pixels)


def world_to_pixel(image_name, lonlats):
    return _transform(image_name, "world-2-pixel", lonlats)


def detect(lat, lon, labels, half_m=40, threshold=0.4, endpoint="ortho/extract"):
    """open-vocabulary detection on vexcel imagery: /elements/detect. labels are free text ("crosswalk").
    returns [{label, confidence, wkt or geometry}] with geometry in lon/lat."""
    region = wkt_point(lon, lat)
    body = {"wkt": region, "srid": 4326, "bbox-dimensions": [2 * half_m, 2 * half_m],
            "images": [{"endpoint": endpoint, "request": {"layer": LAYERS, "wkt": region, "srid": 4326,
                                                          "bbox-dimensions": f"{2 * half_m},{2 * half_m}"}}],
            "model-input": {"elements": [{"label": l, "detection-threshold": threshold} for l in labels],
                            "include": ["geometry", "confidence"]}}
    resp = _call("POST", "/elements/detect", body=body)
    items = _features(resp) or next((resp.get(k) for k in ("detections", "elements", "results")
                                     if isinstance(resp, dict) and isinstance(resp.get(k), list)), [])
    out = []
    for it in items:
        props = it.get("properties", it) if isinstance(it, dict) else {}
        geom = it.get("geometry") if isinstance(it, dict) else None
        out.append({"label": props.get("label"), "confidence": props.get("confidence"),
                    "geometry": geom if isinstance(geom, dict) else None,
                    "wkt": geom if isinstance(geom, str) else props.get("wkt")})
    return out


def geometry_points(det):
    """a detection's geometry as [(lon, lat)], whatever form it came back in."""
    if det.get("geometry"):
        g = det["geometry"]
        c = g.get("coordinates")
        if g.get("type") == "Point":
            return [tuple(c[:2])]
        if g.get("type") in ("LineString", "MultiPoint"):
            return [tuple(p[:2]) for p in c]
        if g.get("type") == "Polygon":
            return [tuple(p[:2]) for p in c[0]]
        if g.get("type") == "MultiPolygon":
            return [tuple(p[:2]) for poly in c for p in poly[0]]
    if det.get("wkt"):
        return [c[:2] for c in parse_wkt_coords(det["wkt"])]
    return []
