"""answers the schema questions routing depends on. run: python -m nudge.probe [model_sid]"""
import json
import sys

from .mp import graph

MODEL = sys.argv[1] if len(sys.argv) > 1 else "zYdLbVFphGC"  # 35th floor office, houston


def fields(type_name):
    t = graph("query($n: String!) { __type(name: $n) { name fields { name type { name kind ofType { name kind } } } } }",
              n=type_name)["__type"]
    if not t:
        return None
    return {f["name"]: (f["type"]["name"] or (f["type"]["ofType"] or {}).get("name")) for f in t["fields"] or []}


def enum(type_name):
    t = graph("query($n: String!) { __type(name: $n) { enumValues { name } } }", n=type_name)["__type"]
    return [v["name"] for v in (t or {}).get("enumValues") or []]


root = graph("{ rootFolder { name modelCountSummary(includeModelsInSubfolders: true) } }")["rootFolder"]
print(f"key ok: {root['name']} ({root['modelCountSummary']} models)\n")

model = fields("Model")
print("Model fields:", sorted(model))
for name in ("panoLocations", "floors", "rooms", "mattertags"):
    t = model.get(name)
    if t:
        print(f"\n{name} -> {t}:", json.dumps(fields(t), indent=None))

edge_t = fields(fields(model["floors"])["edges"]) if model.get("floors") else None
print("\nedge fields:", edge_t)
for guess in ("EdgeType", "FloorEdgeType", "WallType"):
    vals = enum(guess)
    if vals:
        print(f"{guess} values:", vals)

d = graph("""query($id: ID!) { model(id: $id) { name
  floors { label edges { type } }
  rooms { classifications { label } }
  panoLocations { id } } }""", id=MODEL)["model"]
types = {}
for f in d["floors"]:
    for e in f.get("edges") or []:
        types[e.get("type")] = types.get(e.get("type"), 0) + 1
print(f"\n{d['name']}: {len(d['panoLocations'])} panos, edge types {types}")
print("room labels:", sorted({(r.get('classifications') or [{}])[0].get('label') for r in d['rooms']} - {None}))
