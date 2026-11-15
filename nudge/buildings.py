"""find a building by address, name or city in the hackathon models index. shared by the api and voice."""
import csv
import re
from functools import lru_cache

from .demomaps import CORNER, LINE, NAMES
from .mp import ROOT

DEMO_SID = "MgGNVWsyXtH"  # 1255 s atlantic blvd, los angeles: every data layer, dossier, real doorways
VENUE_SID = "R1FX6EmdcDt"  # rabinowitz hall, princeton: the hackathon venue (indoor only; no vexcel there)
HIDDEN = {"synthetic", "synthetic-lobby"}  # test buildings: only reachable by typing the id exactly
# the walk-anywhere demo maps (demomaps.py): found by typing "demo", or the id. never in the featured list
DEMOS = {LINE: (NAMES[LINE], "the straight line demo"), CORNER: (NAMES[CORNER], "the corner demo")}


def _demo_card(sid):
    return {"sid": sid, "address": DEMOS[sid][0], "city": "", "name": DEMOS[sid][0], "rooms": 3, "panos": 0,
            "floors": 1, "has_costar": False, "has_dossier": False}


@lru_cache(maxsize=1)
def _extra():
    """models outside the hackathon index (the venue scan), same columns as models_index.csv."""
    p = ROOT / "reference" / "extra_models.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("sid")]


def extra_row(sid):
    return next((r for r in _extra() if r["sid"] == sid), None)


@lru_cache(maxsize=1)
def _rows():
    extra = _extra()
    seen = {r["sid"] for r in extra}  # an extra row replaces the index row for the same model
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        return extra + [r for r in csv.DictReader(f) if r.get("lat") and r["sid"] not in seen]


def _n(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _street(r):
    a = (r.get("address") or "").split(",")[0].strip()
    return a if a and a.upper() != "US" else (r.get("name") or "").strip()


def card(r):
    sid = r["sid"]
    return {"sid": sid, "address": _street(r), "city": (r.get("city") or "").split(",")[0].strip(),
            "name": r.get("name") or "", "rooms": _n(r.get("room_count")), "panos": _n(r.get("pano_count")),
            "floors": _n(r.get("floor_count")), "has_costar": bool(r.get("costar_property_id")),
            "has_dossier": (ROOT / "cache" / "dossiers" / sid / "annotations.json").exists()}


def _richness(r):
    return min(_n(r.get("pano_count")), 80) + 3 * min(_n(r.get("room_count")), 25) + 30 * bool(r.get("costar_property_id"))


def featured(limit=6):
    from .pick import score
    rows = _rows()
    first = (DEMO_SID, VENUE_SID)  # the full-data demo, then the venue (indoor only)
    demo = [next(r for r in rows if r["sid"] == s) for s in first if any(r["sid"] == s for r in rows)]
    rest = [r for r in rows if r["sid"] not in first and r["segment"] == "commercial" and r.get("costar_property_id")
            and _street(r) and not re.match(r"^\d+BR|^\d+BD", r.get("name") or "")]
    rest.sort(key=score, reverse=True)
    return [card(r) for r in demo + rest[:max(0, limit - len(demo))]]


def search(q, limit=12):
    q = (q or "").strip()
    if not q:
        return featured(limit)
    if q in HIDDEN:
        return [{"sid": q, "address": "Test building", "city": "", "name": q, "rooms": 0, "panos": 0, "floors": 2,
                 "has_costar": False, "has_dossier": False}]
    if q in DEMOS:
        return [_demo_card(q)]
    if q.lower().startswith("demo"):
        rest = re.findall(r"[a-z0-9]+", q.lower())[1:]   # "demo corner" narrows it to the one
        return [_demo_card(s) for s, (name, _) in DEMOS.items() if all(t in name.lower() for t in rest)][:limit]
    toks = [t for t in re.findall(r"[a-z0-9]+", q.lower()) if t not in ("the", "st", "street", "blvd", "ave")] or \
        re.findall(r"[a-z0-9]+", q.lower())
    hits = []
    for r in _rows():
        hay = " ".join([r.get("address") or "", r.get("name") or "", r.get("city") or "", r["sid"]]).lower()
        words = set(re.findall(r"[a-z0-9]+", hay))
        if all(t in words or any(w.startswith(t) for w in words) for t in toks):
            if _street(r).lower() == (r.get("city") or "").split(",")[0].strip().lower():
                continue  # no street address, just the city: useless as a pick
            exact = sum(t in words for t in toks)
            # an address-like query ("1255 atlantic") prefers the street; a city word prefers richer scans
            addressy = any(ch.isdigit() for ch in q) or len(toks) > 1
            street_hit = addressy and all(t in _street(r).lower() for t in toks)
            venue = r["sid"] == VENUE_SID  # "princeton" should find the building the judges are standing in
            hits.append((-(exact + 2 * street_hit + 3 * venue), -_richness(r), r))
    hits.sort(key=lambda h: h[:2])
    seen, out = set(), []
    for *_, r in hits:
        key = (_street(r).lower(), (r.get("city") or "").lower())
        if key in seen:  # several scans of one address: keep the richest
            continue
        seen.add(key)
        out.append(card(r))
        if len(out) >= limit:
            break
    return out


def label(sid):
    """'1255 S Atlantic Blvd, Los Angeles' for a sid."""
    if sid in HIDDEN:
        return "the test building"
    if sid in DEMOS:
        return DEMOS[sid][1]
    r = next((r for r in _rows() if r["sid"] == sid), None)
    if not r:
        return sid
    c = card(r)
    return f"{c['address']}, {c['city']}" if c["city"] else c["address"]
