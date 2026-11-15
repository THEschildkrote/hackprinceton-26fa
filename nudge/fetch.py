"""pull everything walkgraph.build needs for one model, with a disk cache."""
import json
import re

from .mp import ROOT, MatterportError, graph

CACHE = ROOT / "cache" / "models"

# fields we're not sure the schema has; stripped in this order when the api complains
OPTIONAL = {
    "sequence": "sequence",
    "pano_floor": "floor { id }",
    "pano_room": "room { id }",
    "thickness": "thickness",
    "dimensions": "dimensions(units: metric) { areaFloor width depth height }",
    "internalId": "internalId",
}


def q_walk(drop=()):
    def opt(k):
        return "" if k in drop else OPTIONAL[k]
    return f"""query Walk($id: ID!) {{ model(id: $id) {{
  id name {opt("internalId")}
  address {{ address locality }}
  geolocation {{ lat long }}
  floors {{ id label {opt("sequence")}
    vertices {{ id position {{ x y }} }}
    edges {{ id type {opt("thickness")} vertices {{ id }} }} }}
  rooms {{ id floor {{ id }} classifications {{ label confidence }} boundary {{ edges {{ id }} }}
    {opt("dimensions")} }}
  panoLocations {{ id label position {{ x y z }} {opt("pano_floor")} {opt("pano_room")} }}
}} }}"""


Q_WALK = q_walk()


def _culprit(msg, drop):
    """which optional field an error message is about, if any."""
    m = msg.lower()
    # quoted lowercase names are fields/args; capitalized ones are types like "Room"
    names = [n for n in re.findall(r"['\"]([A-Za-z_]+)['\"]", msg) if n[0].islower()]
    for k in OPTIONAL:
        if k in drop:
            continue
        word = {"pano_floor": "floor", "pano_room": "room"}.get(k, k)
        if word.lower() in [n.lower() for n in names] or (not names and word.lower() in m):
            return k
    return None


def fixture(name):
    """tests/<name>.py by file path, so it loads even where another package is called "tests" (langgraph dev)."""
    import importlib.util
    import sys
    key = f"tests.{name}"
    if key in sys.modules:
        return sys.modules[key]
    try:
        return importlib.import_module(key)
    except ImportError:
        spec = importlib.util.spec_from_file_location(key, ROOT / "tests" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
        return mod


FT = 0.3048


def _floor_elevations(sid):
    """floor id -> elevation in metres, the median of its rooms' lowerElevation. panos often don't say their
    floor, and a floor with no panos (an unscanned basement) breaks the z-gap split."""
    m = graph("""query($id: ID!) { model(id: $id) { rooms { floor { id } dimensionEstimates { lowerElevation units } } } }""",
              id=sid)["model"]
    by = {}
    for r in m.get("rooms") or []:
        d = r.get("dimensionEstimates") or {}
        if d.get("lowerElevation") is None:
            continue
        v = d["lowerElevation"] * (FT if (d.get("units") or "").lower() == "imperial" else 1.0)
        by.setdefault(r["floor"]["id"], []).append(v)
    return {f: sorted(vs)[len(vs) // 2] for f, vs in by.items()}


def _enrich(sid, model):
    """fill in floor elevations and a missing geolocation (from reference/extra_models.csv). true if changed."""
    changed = False
    floors = model.get("floors") or []
    if floors and "_elevations" not in model:
        try:
            el = _floor_elevations(sid)
        except Exception:
            el = {}
        for f in floors:
            if f["id"] in el:
                f["elevation_m"] = round(el[f["id"]], 3)
        model["_elevations"] = bool(el)
        changed = True
    from .buildings import extra_row
    r = extra_row(sid)
    names = dict(kv.split("=", 1) for kv in (r or {}).get("floor_names", "").split(";") if "=" in kv)
    for f in floors:  # spoken names ("upper floor") where matterport's numbering misleads (unscanned basement)
        if names.get(f["id"]) and f.get("label") != names[f["id"]]:
            f["label"] = names[f["id"]]
            changed = True
    if not model.get("geolocation"):
        if r and r.get("lat"):
            model["geolocation"] = {"lat": r["lat"], "long": r["lon"]}
            if r.get("address"):
                model["address"] = {"address": r["address"], "locality": r.get("city") or ""}
            changed = True
    return changed


def load(sid, refresh=False):
    if sid == "synthetic":
        return fixture("synthetic").building()
    if sid == "synthetic-lobby":  # a second scan of the synthetic building (multi-model tests and demo)
        return fixture("synthetic_building").lobby()
    from . import demomaps
    if sid in demomaps.NAMES:  # the walk-anywhere demo maps: a straight line and one corner
        return demomaps.model(sid)
    path = CACHE / f"{sid}.json"
    if path.exists() and not refresh:
        model = json.loads(path.read_text(encoding="utf-8"))
        if _enrich(sid, model):
            path.write_text(json.dumps(model), encoding="utf-8")
        return model
    drop = []
    while True:
        try:
            model = graph(q_walk(drop), id=sid)["model"]
            break
        except MatterportError as ex:
            k = _culprit(str(ex), drop)
            if not k:
                raise
            drop.append(k)
    model["_dropped"] = drop
    _enrich(sid, model)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model), encoding="utf-8")
    return model
