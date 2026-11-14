"""thin clients for the matterport model api and vexcel."""
import math
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.matterport.com/api/models/graph"
VEXCEL = "https://api.vexcelgroup.com/v2"
M_PER_DEG = 111_320


def secret(name, required=True):
    value = os.environ.get(name)
    env = ROOT / ".env"
    if not value and env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name:
                value = v.strip().strip('"').strip("'")
    if not value and required:
        raise SystemExit(f"{name} is not set (put it in .env)")
    return value


_session = None


def _s():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.auth = (secret("MATTERPORT_API_KEY"), secret("MATTERPORT_API_TOKEN"))
    return _session


class MatterportError(RuntimeError):
    pass


def graph(query, **variables):
    r = _s().post(API, json={"query": query, "variables": variables}, timeout=120)
    if r.status_code in (401, 403):
        raise MatterportError(f"key rejected (HTTP {r.status_code})")
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise MatterportError("; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


def square(lat, lon, half_m):
    # wkt is lon lat order
    dlat = half_m / M_PER_DEG
    dlon = half_m / (M_PER_DEG * math.cos(math.radians(lat)))
    w, e, s, n = lon - dlon, lon + dlon, lat - dlat, lat + dlat
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def vexcel_ortho(lat, lon, half_m=70, px=1400):
    r = requests.post(f"{VEXCEL}/ortho/extract", headers={"X-API-KEY": secret("VEXCEL_API_KEY")}, timeout=300, json={
        "wkt": square(lat, lon, half_m), "srid": 4326, "layer": "urban,urban-r,wide-area,urban-g",
        "crop": "bounding_box", "image-format": "jpeg", "destination-dimensions": f"{px},{px}", "attribution": "false"})
    r.raise_for_status()
    return r.content
