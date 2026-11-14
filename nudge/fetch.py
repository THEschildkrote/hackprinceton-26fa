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


def load(sid, refresh=False):
    if sid == "synthetic":
        return fixture("synthetic").building()
    if sid == "synthetic-lobby":  # a second scan of the synthetic building (multi-model tests and demo)
        return fixture("synthetic_building").lobby()
    path = CACHE / f"{sid}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
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
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model), encoding="utf-8")
    return model
