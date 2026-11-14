"""a fake city block around tests/synthetic.building(), with mocked vexcel and overpass responses.

layout in the building's model frame (the lobby's street wall is y = 0), then rotated 30 deg and shifted (35, 20) m
into local east/north, then to lon/lat around a point in los angeles:

  far sidewalk   f0(-10,-14) -- f1(0,-14) ------------------ f2(12,-14)
  crossing 1     f1 -> c1 -> k1(0,-4.6, raised curb) -> s1   (no signal)
  crossing 2     f2 -> c2 -> k2(12,-4.6, curb ramp) -> s3    (signals)
  near sidewalk  s1(0,-4) -- s2(3.5,-4) -- s3(12,-4) -- s4(20,-4)
  entrance walk  s2 -> e0(3.5,-0.8)            gentle 3% ramp up
  plaza path     s3 -> p(8,-1) -> e0           over a berm, 6-7% (steep, not too steep)
  a pole on the near sidewalk at (6,-4); a painted crosswalk at crossing 2.
"""
import io
import json
import math
import random

from PIL import Image

from nudge import walkgraph
from nudge.georef import ENU, outline
from tests.synthetic import building

LAT0, LON0 = 34.05, -118.25
TH, TX, TY = 30.0, 35.0, 20.0

NODES = {  # name: (osm id, x, y) in the model frame
    "f0": (100, -10, -14), "f1": (101, 0, -14), "f2": (102, 12, -14),
    "c1": (110, 0, -9), "k1": (111, 0, -4.6), "c2": (120, 12, -9), "k2": (121, 12, -4.6),
    "s1": (130, 0, -4), "s2": (131, 3.5, -4), "s3": (132, 12, -4), "s4": (133, 20, -4),
    "e0": (140, 3.5, -0.8), "p": (141, 8, -1.0),
}
WAYS = [
    (1, ["f0", "f1", "f2"], {"highway": "footway", "footway": "sidewalk"}),
    (2, ["f1", "c1", "k1", "s1"], {"highway": "footway", "footway": "crossing", "crossing": "uncontrolled"}),
    (3, ["f2", "c2", "k2", "s3"], {"highway": "footway", "footway": "crossing", "crossing": "traffic_signals"}),
    (4, ["s1", "s2", "s3", "s4"], {"highway": "footway", "footway": "sidewalk"}),
    (5, ["s2", "e0"], {"highway": "footway"}),
    (6, ["s3", "p", "e0"], {"highway": "footway"}),
]
NODE_TAGS = {
    "c1": {"highway": "crossing", "crossing": "uncontrolled"},
    "c2": {"highway": "crossing", "crossing": "traffic_signals"},
    "k1": {"barrier": "kerb", "kerb": "raised"},
    "k2": {"barrier": "kerb", "kerb": "lowered"},
    "e0": {"entrance": "main"},
}


def oid(name):
    return f"out:{NODES[name][0]}"


class Block:
    def __init__(self, noise=0.08, seed=1):
        self.enu = ENU(LAT0, LON0)
        self.gi = walkgraph.build(building())
        c, s = math.cos(math.radians(TH)), math.sin(math.radians(TH))
        self.model_to_enu = lambda x, y: (c * x - s * y + TX, s * x + c * y + TY)
        self.enu_to_model = lambda e, n: (c * (e - TX) + s * (n - TY), -s * (e - TX) + c * (n - TY))
        rnd = random.Random(seed)
        ring = outline(self.gi)
        self.footprint_enu = [(e + rnd.gauss(0, noise), n + rnd.gauss(0, noise))
                              for e, n in (self.model_to_enu(*p) for p in ring)]
        self.footprint_lonlat = [self.enu.to_lonlat(*p) for p in self.footprint_enu]
        self.calls = []

    def lonlat(self, name):
        _, x, y = NODES[name]
        return self.enu.to_lonlat(*self.model_to_enu(x, y))

    def latlon(self, name):
        lon, lat = self.lonlat(name)
        return lat, lon

    def z_model(self, x, y):
        apron = 0.1 * min(1.0, max(0.0, (y + 4) / 3.2))
        cone = 0.16 * max(0.0, 1 - math.dist((x, y), (8, -1.0)) / 3.0)
        return apron + cone

    def elevation(self, lonlats):
        return [self.z_model(*self.enu_to_model(*self.enu.to_enu(lon, lat))) for lon, lat in lonlats]

    def osm(self):
        els = []
        for wid, names, tags in WAYS:
            els.append({"type": "way", "id": wid, "tags": tags, "nodes": [NODES[n][0] for n in names],
                        "geometry": [dict(zip(("lon", "lat"), self.lonlat(n))) for n in names]})
        for name, tags in NODE_TAGS.items():
            lon, lat = self.lonlat(name)
            els.append({"type": "node", "id": NODES[name][0], "lat": lat, "lon": lon, "tags": tags})
        return {"elements": els}

    def detections(self):
        pole = self.enu.to_lonlat(*self.model_to_enu(6.0, -4.0))
        cw = [self.enu.to_lonlat(*self.model_to_enu(x, y)) for x, y in ((11, -10), (13, -10), (13, -8), (11, -8), (11, -10))]
        return [{"label": "pole", "confidence": 0.81, "geometry": {"type": "Point", "coordinates": list(pole)}},
                {"label": "crosswalk", "confidence": 0.77, "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in cw]]}}]

    # fake http, at the client boundary

    def overpass_transport(self, query):
        self.calls.append(("overpass", query))
        return self.osm()

    def transport(self, method, path, params, body):
        from nudge.vexcel import parse_wkt_coords
        self.calls.append((method, path, params, body))
        if path == "/structure/geometry/extract":
            ring = [list(p) for p in self.footprint_lonlat] + [list(self.footprint_lonlat[0])]
            fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                                                             "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
            return 200, "application/json", json.dumps(fc).encode()
        if path == "/dtm/elevation":
            pts = [c[:2] for c in parse_wkt_coords(params["wkt"])]
            zs = self.elevation(pts)
            kind = "POINT Z" if len(pts) == 1 else "LINESTRING Z"
            wkt = f"{kind} (" + ", ".join(f"{x} {y} {z}" for (x, y), z in zip(pts, zs)) + ")"
            return 200, "application/json", json.dumps({"wkt": wkt, "srid": 4326, "collection": ["fake"]}).encode()
        if path == "/elements/detect":
            fc = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"label": d["label"], "confidence": d["confidence"]}, "geometry": d["geometry"]}
                for d in self.detections()]}
            return 200, "application/json", json.dumps(fc).encode()
        if path == "/oriented/extract":
            buf = io.BytesIO()
            Image.new("RGB", (64, 64), (120, 110, 100)).save(buf, "JPEG")
            return 200, "image/jpeg", buf.getvalue()
        if path == "/oriented/query":
            fc = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"image-name": f"img_{d}", "product-type": f"oblique-{d}"}, "geometry": None}
                for d in ("north", "south", "east", "west")]}
            return 200, "application/json", json.dumps(fc).encode()
        if path == "/oriented/transform-points":
            pts = [c[:2] for c in parse_wkt_coords(params["wkt"])]
            return 200, "application/json", json.dumps({"points": [{"x": x, "y": y} for x, y in pts]}).encode()
        return 404, "application/json", b'{"status":404}'
