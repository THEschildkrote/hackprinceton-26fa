"""rank commercial models that join to costar and sit under vexcel coverage. run: python -m nudge.pick"""
import csv
import re

from .mp import ROOT

# lobbies, entrances, corridors and elevators make better wayfinding demos than empty suites
GOOD = re.compile(r"lobby|entr|floor|level|suite|office|medical|clinic|retail|building", re.I)


def load():
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def n(x):
    return int(x) if x else 0


def score(r):
    s = min(n(r["pano_count"]), 80) + 3 * min(n(r["room_count"]), 25) + 25 * (n(r["floor_count"]) - 1)
    s += 40 * (r["splat"] == "yes") + 15 * (r["ai_summary"] == "yes") + 20 * bool(GOOD.search(r["name"]))
    return s


if __name__ == "__main__":
    rows = [r for r in load() if r["segment"] == "commercial" and r["costar_property_id"] and r["lat"]]
    rows.sort(key=score, reverse=True)
    print(f"{len(rows)} commercial models with a costar id and lat/lon\n")
    for r in rows[:30]:
        print(f"{score(r):4d}  {r['sid']}  {r['name'][:34]:34}  {r['city'][:11]:11}  "
              f"panos {n(r['pano_count']):3d}  rooms {n(r['room_count']):2d}  floors {r['floor_count']}  "
              f"splat {r['splat']:3}  costar {r['costar_property_id']}")
