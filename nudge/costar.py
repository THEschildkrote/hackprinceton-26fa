"""the costar kit: a password-protected zip of jsonl (and csv) records plus docs and schemas.

lookup(sid) joins a matterport model to its record. the kit's own join tables (commercial-properties.csv,
residential-listings.csv) map MatterportModelId to a property or listing id; reference/models_index.csv is the
fallback. the full record comes from data/commercial-property.jsonl (or residential-listings.jsonl), with
current tenants (location-occupancy + company-detail) and the active listing (commercial-listing-summary)
attached under record["_related"]. enum ints are decoded with schemas/enums.json.

the kit is extracted once under cache/costar_kit/ (gitignored) and indexed by id -> byte offsets, so a lookup
is a few seeks. env (.env): COSTAR_KIT_ZIP or COSTAR_KIT_URL (downloaded once), COSTAR_ZIP_PASSWORD.
"""
import csv
import json
import re
import zipfile
from functools import lru_cache

import requests

from .mp import ROOT, secret

KIT_DIR = ROOT / "cache" / "costar_kit"
AES_METHOD = 99  # winzip aes; python's zipfile only does zipcrypto
ID_KEY = re.compile(r"(property.?id|listing.?id|^id$|costar.?id|propertyid|listingid)", re.I)
INDEX_VERSION = 2
SQFT_M2 = 0.09290304

# files we know the shape of: basename -> fields to index (value -> [byte offsets])
KNOWN = {
    "commercial-property.jsonl": ("id",),
    "location-occupancy.jsonl": ("propertyId",),
    "company-detail.jsonl": ("id",),
    "commercial-listing-summary.jsonl": ("propertyId",),
    "residential-listings.jsonl": ("id",),
    "residential-listing-features.jsonl": ("id",),
}
# the kit's own join tables: matterport model id -> property / listing id
JOINS = {"commercial-properties.csv": ("commercial", "PropertyId"), "residential-listings.csv": ("residential", "ListingId")}

# field name -> enum name in schemas/enums.json
ENUM_FIELDS = {
    "propertyType": "PropertyType", "propertySubtype": "PropertySubtype", "buildingClass": "BuildingClass",
    "coStarBuildingType": "CoStarBuildingType", "parkingType": "ParkingType", "eventType": "ConstructionEventType",
    "sprinklerType": "SprinklerType", "tenancyType": "TenancyType", "elevatorType": "ElevatorType",
    "industryType": "IndustryType", "leaseType": "LeaseType", "listingStatus": "ListingStatus", "listingType": "ListingType",
    "floorName": "FloorName", "buildingRating": "BuildingRating", "constructionStatus": "ConstructionStatus",
}
BUILT = {"ConstructionComplete"}
RENOVATED = {"RenovationFinished", "CommonAreaRenovated", "RedevelopmentComplete", "Redeveloped"}
ADA_YEAR = 1993  # new construction first occupied after january 26, 1993 must comply
ACCESS_WORDS = re.compile(r"elevator|lift|ramp|accessib|ada|wheelchair|handicap|parking|transit|braille", re.I)


class KitError(RuntimeError):
    pass


def _paths():
    return {"zip": KIT_DIR / "kit.zip", "data": KIT_DIR / "data", "docs": KIT_DIR / "docs",
            "index": KIT_DIR / "index.json"}


def kit_zip():
    """local path of the kit zip, downloading it once if only a url is configured."""
    p = secret("COSTAR_KIT_ZIP", required=False)
    if p:
        from pathlib import Path
        if not Path(p).exists():
            raise KitError("COSTAR_KIT_ZIP points to a file that doesn't exist")
        return Path(p)
    z = _paths()["zip"]
    if z.exists():
        return z
    url = secret("COSTAR_KIT_URL", required=False)
    if not url:
        raise KitError("no CoStar kit: set COSTAR_KIT_URL (from the CoStar keys card) or COSTAR_KIT_ZIP")
    KIT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = z.with_suffix(".part")
    with requests.get(url, stream=True, timeout=600) as r:
        if r.status_code in (401, 403):
            raise KitError(f"the kit link was refused (HTTP {r.status_code}); it may have expired, get a fresh one")
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(z)
    return z


def is_aes(info):
    return info.compress_type == AES_METHOD


def open_kit(path=None):
    """a zipfile-like object with the password set; pyzipper for aes kits if it's installed."""
    path = path or kit_zip()
    pw = secret("COSTAR_ZIP_PASSWORD", required=False)
    zf = zipfile.ZipFile(path)
    if any(is_aes(i) for i in zf.infolist()):
        zf.close()
        try:
            import pyzipper
        except ImportError:
            raise KitError("the kit uses AES encryption; run: pip install pyzipper")
        zf = pyzipper.AESZipFile(path)
    if any(i.flag_bits & 0x1 for i in zf.infolist()):
        if not pw:
            raise KitError("the kit is encrypted: set COSTAR_ZIP_PASSWORD (type it, don't paste, per the track tips)")
        zf.setpassword(pw.encode())
    return zf


def _read(zf, name):
    try:
        return zf.read(name)
    except RuntimeError as ex:  # bad password
        raise KitError(f"the kit rejected COSTAR_ZIP_PASSWORD ({ex.__class__.__name__}); re-enter it with "
                       f"python -m nudge.setkey COSTAR_ZIP_PASSWORD")


def extract(path=None):
    """decrypt the docs and data once. returns {"docs": [...], "data": [...]}"""
    P = _paths()
    zf = open_kit(path)
    out = {"docs": [], "data": []}
    with zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            low = name.lower()
            if low.endswith((".md", ".txt")):
                dest, kind = P["docs"] / name, "docs"
            elif low.endswith((".jsonl", ".json", ".ndjson", ".csv", ".yaml", ".yml")):
                dest, kind = P["data"] / name, "data"
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists() or dest.stat().st_size != info.file_size:
                dest.write_bytes(_read(zf, name))
            out[kind].append(name)
    return out


def docs():
    """the kit's own documentation, so field names come from it, not from guesses."""
    d = _paths()["docs"]
    return {str(p.relative_to(d)): p.read_text(encoding="utf-8", errors="replace") for p in sorted(d.rglob("*"))
            if p.is_file()} if d.exists() else {}


def field_hints():
    """field-like names the docs mention (backticked or first table column)."""
    names = set()
    for text in docs().values():
        names |= set(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{2,60})`", text))
        names |= {m.strip() for m in re.findall(r"^\|\s*([A-Za-z_][A-Za-z0-9_.]{2,60})\s*\|", text, re.M)}
    return sorted(names)


# enums

def _find(name):
    """a file anywhere under the extracted data (the zip may nest data/ inside data/)."""
    d = _paths()["data"]
    if not d.exists():
        return None
    hits = sorted(d.rglob(name), key=lambda p: len(p.parts))
    return hits[0] if hits else None


@lru_cache(maxsize=4)
def _enums_at(path):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return {}


def enums():
    p = _find("enums.json")
    return _enums_at(str(p)) if p else {}


def decode(enum_name, value):
    """enum int -> name, or the value unchanged when the enum or the code is unknown."""
    table = enums().get(enum_name) or {}
    if isinstance(value, list):
        return [decode(enum_name, v) for v in value]
    return table.get(str(value), value) if value is not None else None


def decoded(record):
    """a copy of the record with known enum fields decoded to names (nested too)."""
    if isinstance(record, list):
        return [decoded(x) for x in record]
    if not isinstance(record, dict):
        return record
    out = {}
    for k, v in record.items():
        if k in ENUM_FIELDS and not isinstance(v, (dict, list)):
            out[k] = decode(ENUM_FIELDS[k], v)
        else:
            out[k] = decoded(v)
    return out


# index

def _kind(name):
    low = name.lower()
    if any(w in low for w in ("residential", "homes", "listing")) and "commercial" not in low:
        return "residential"
    return "commercial"


def _ids(rec):
    """top-level and one-level-nested id values worth indexing (unknown file shapes only)."""
    out = []
    for k, v in rec.items():
        if isinstance(v, (str, int)) and ID_KEY.search(k):
            out.append(str(v))
        elif isinstance(v, dict):
            out += [str(x) for kk, x in v.items() if isinstance(x, (str, int)) and ID_KEY.search(kk)]
    return out


def _scan_lines(f):
    with open(f, "rb") as fh:
        while True:
            off = fh.tell()
            line = fh.readline()
            if not line:
                return
            line = line.strip()
            if line.startswith(b"{"):
                try:
                    yield off, json.loads(line)
                except json.JSONDecodeError:
                    continue


def build_index(refresh=False):
    """{"version", "ids": id -> [file, offset, kind], "by": file -> field -> value -> [offsets],
    "join": kind -> matterport model id -> property/listing id}."""
    P = _paths()
    if P["index"].exists() and not refresh:
        idx = json.loads(P["index"].read_text(encoding="utf-8"))
        if isinstance(idx, dict) and idx.get("version") == INDEX_VERSION:
            return idx
    if not P["data"].exists() or not any(p.is_file() for p in P["data"].rglob("*")):
        extract()
    ids, by, join = {}, {}, {"commercial": {}, "residential": {}}
    csv_hits = {}
    for f in sorted(p for p in P["data"].rglob("*") if p.is_file()):
        rel, base, kind = f.relative_to(P["data"]).as_posix(), f.name, _kind(f.name)
        suffix = f.suffix.lower()
        if suffix == ".csv":
            with open(f, "rb") as fh:
                header = next(csv.reader([fh.readline().decode("utf-8", "replace")]), [])
                hmap = {h: i for i, h in enumerate(header)}
                if base in JOINS and "MatterportModelId" in hmap and JOINS[base][1] in hmap:
                    jk, col = JOINS[base]
                    for line in fh:
                        row = next(csv.reader([line.decode("utf-8", "replace")]), [])
                        if len(row) > max(hmap["MatterportModelId"], hmap[col]):
                            join[jk][row[hmap["MatterportModelId"]]] = row[hmap[col]]
                    continue
                cols = [i for i, h in enumerate(header) if ID_KEY.search(h)]
                while True:
                    off = fh.tell()
                    line = fh.readline()
                    if not line:
                        break
                    row = next(csv.reader([line.decode("utf-8", "replace")]), [])
                    for i in cols:
                        if i < len(row) and row[i]:
                            csv_hits.setdefault(row[i], [rel, off, kind])
            continue
        if suffix not in (".jsonl", ".ndjson"):
            continue
        if base in KNOWN:
            fields = KNOWN[base]
            slot = by.setdefault(rel, {fld: {} for fld in fields})
            for off, rec in _scan_lines(f):
                for fld in fields:
                    v = rec.get(fld)
                    if v is not None:
                        slot[fld].setdefault(str(v), []).append(off)
            continue
        for off, rec in _scan_lines(f):
            for v in _ids(rec):
                ids.setdefault(v, [rel, off, kind])
    for k, v in csv_hits.items():  # jsonl records win over csv rows
        ids.setdefault(k, v)
    idx = {"version": INDEX_VERSION, "ids": ids, "by": by, "join": join}
    KIT_DIR.mkdir(parents=True, exist_ok=True)
    P["index"].write_text(json.dumps(idx), encoding="utf-8")
    return idx


def _record_at(rel, off):
    f = _paths()["data"] / rel
    with open(f, "rb") as fh:
        if f.suffix.lower() == ".csv":
            header = next(csv.reader([fh.readline().decode("utf-8", "replace")]))
            fh.seek(off)
            row = next(csv.reader([fh.readline().decode("utf-8", "replace")]))
            return dict(zip(header, row))
        fh.seek(off)
        return json.loads(fh.readline())


def _by(idx, base, field, value):
    """records in the known file `base` whose `field` equals value."""
    for rel, fields in idx.get("by", {}).items():
        if rel.rsplit("/", 1)[-1] == base and field in fields:
            return [_record_at(rel, off) for off in fields[field].get(str(value), [])]
    return []


def _index_row(sid):
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["sid"] == sid:
                return r
    return None


SYNTHETIC = {"propertyId": "synthetic", "yearBuilt": 1984, "numberOfStories": 2, "elevators": 1,
             "parking": {"spaces": 40, "accessibleSpaces": 2, "type": "surface lot"},
             "buildingClass": "B", "propertyType": "Office",
             "tenants": [{"name": "Acme Dental", "suite": "Office"}], "amenities": ["Conference Room"]}


def _floor_names(record):
    return {f.get("propertyFloorId"): decode("FloorName", f.get("floorName")) for f in record.get("floors") or []
            if isinstance(f, dict)}


def tenants(idx, record):
    """current tenants of a commercial property: occupancies with no toDate, names from company-detail."""
    pid = record.get("id")
    if pid is None:
        return []
    floors = _floor_names(record)
    out = []
    for occ in _by(idx, "location-occupancy.jsonl", "propertyId", pid):
        if occ.get("toDate"):
            continue
        comp = None
        if occ.get("tenantLocationId") is not None:
            hits = _by(idx, "company-detail.jsonl", "id", occ["tenantLocationId"])
            comp = hits[0] if hits else None
        prof = (comp or {}).get("profile") or {}
        spaces = [s for s in occ.get("spaces") or [] if isinstance(s, dict)]
        out.append({
            "name": prof.get("companyName") or (comp or {}).get("name") or "unknown tenant",
            "industry": decode("IndustryType", prof["industryType"] if prof.get("industryType") is not None else occ.get("industryType")),
            "suite": next((s.get("suite") for s in spaces if s.get("suite")), None),
            "floor": next((floors.get(s.get("propertyFloorId")) for s in spaces if s.get("propertyFloorId") in floors), None),
            "sqft": round(sum(s.get("squareFeetOccupied") or 0 for s in spaces)) or None,
            "since": (occ.get("moveInDate") or occ.get("fromDate") or "")[:10] or None,
        })
    return out


def active_listing(idx, record):
    """the property's active listing summary (listingStatus Active), trimmed to scalar fields."""
    pid = record.get("id")
    if pid is None:
        return None
    rows = _by(idx, "commercial-listing-summary.jsonl", "propertyId", pid)
    active = [r for r in rows if decode("ListingStatus", r.get("listingStatus")) == "Active"] or rows
    if not active:
        return None
    r = active[0]
    keep = {k: v for k, v in r.items() if not isinstance(v, (dict, list)) and v not in (None, "")}
    return decoded(keep)


def lookup(sid):
    """{sid, key, kind, file, record} or {sid, error}. record["_related"] holds tenants and listing."""
    if sid.startswith("synthetic"):
        return {"sid": sid, "key": "synthetic", "kind": "commercial", "file": None, "record": SYNTHETIC}
    row = _index_row(sid) or {}
    try:
        idx = build_index()
    except KitError as ex:
        key = row.get("costar_property_id") or row.get("homes_listing_id")
        return {"sid": sid, "key": key, "error": str(ex)}
    join = idx.get("join", {})
    pid = join.get("commercial", {}).get(sid) or row.get("costar_property_id")
    lid = join.get("residential", {}).get(sid) or row.get("homes_listing_id")
    if not (pid or lid or row):
        return {"sid": sid, "error": "not in models_index.csv or the kit's join tables"}
    if not (pid or lid):
        return {"sid": sid, "error": "this model has no CoStar property id or Homes listing id"}
    if pid:
        recs = _by(idx, "commercial-property.jsonl", "id", pid)
        if recs:
            rec = recs[0]
            rec["_related"] = {"tenants": tenants(idx, rec), "listing": active_listing(idx, rec)}
            return {"sid": sid, "key": str(pid), "kind": "commercial", "file": "commercial-property.jsonl", "record": rec}
    if lid:
        recs = _by(idx, "residential-listings.jsonl", "id", lid)
        if recs:
            rec = recs[0]
            feats = _by(idx, "residential-listing-features.jsonl", "id", lid)
            rec["_related"] = {"features": feats[0] if feats else None}
            return {"sid": sid, "key": str(lid), "kind": "residential", "file": "residential-listings.jsonl", "record": rec}
    key = str(pid or lid)
    hit = idx.get("ids", {}).get(key)
    if not hit:
        return {"sid": sid, "key": key, "error": f"id {key} is not in the kit"}
    rel, off, kind = hit
    return {"sid": sid, "key": key, "kind": kind, "file": rel, "record": _record_at(rel, off)}


def _flatten(o, prefix=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        yield prefix, o


def _walk_key(o, key):
    """every value stored under `key` anywhere in a nested record."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from _walk_key(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_key(v, key)


def _is_real(record):
    return isinstance(record, dict) and any(k in record for k in ("constructionEvents", "numStoriesAboveGrade",
                                                                    "primaryAddress", "coStarBuildingType"))


def real_facts(record):
    """facts from the real commercial-property schema (enum ints decoded)."""
    facts = {}
    events = [e for e in record.get("constructionEvents") or [] if isinstance(e, dict) and e.get("year")]
    names = [(decode("ConstructionEventType", e.get("eventType")), int(e["year"])) for e in events]
    built = [y for n, y in names if n in BUILT] or [y for n, y in names if n in ("GroundBreaking", "Unknown", 0)]
    reno = [y for n, y in names if n in RENOVATED]
    parts = []
    for r in record.get("renovations") or []:
        if not isinstance(r, dict):
            continue
        y = _year(r.get("endDate") or r.get("startDate") or r.get("year") or r.get("yearRenovated"))
        if y:
            reno.append(y)
        for t in r.get("renovationImprovementTypes") or []:
            name = decode("RenovationImprovementType", t)
            if isinstance(name, str) and name not in parts and name != "Unknown":
                parts.append(name)
    if built:
        facts["year_built"] = min(built)
    if reno:
        facts["year_renovated"] = max(reno)
    if parts:
        facts["renovated_parts"] = parts
    if events:
        facts["construction_events"] = [{"type": n, "year": y} for n, y in names]
    if record.get("numStoriesAboveGrade") is not None:
        facts["floors"] = record["numStoriesAboveGrade"]
    fl = [decode("FloorName", f.get("floorName")) for f in record.get("floors") or [] if isinstance(f, dict)]
    if fl:
        facts["floor_names"] = fl
    elev = []
    for lst in _walk_key(record, "elevators"):
        for e in lst if isinstance(lst, list) else []:
            if isinstance(e, dict):
                elev.append({"type": decode("ElevatorType", e.get("elevatorType")), "count": e.get("numElevators"),
                             "exists": {1: True, 2: False}.get(e.get("elevatorState"))})
    if elev:
        facts["elevators"] = elev
        facts["passenger_elevators"] = sum(e["count"] or 0 for e in elev if e["type"] == "Passenger" and e["exists"] is not False)
    parking = [{"type": decode("ParkingType", p.get("parkingType")), "spaces": p.get("totalSpaces")}
               for p in record.get("parking") or [] if isinstance(p, dict)]
    if parking:
        facts["parking"] = {"total": sum(p["spaces"] or 0 for p in parking), "by_type": parking}
        if record.get("parkingRatio") is not None:
            facts["parking"]["per_1000_sqft"] = record["parkingRatio"]
    acc = [(p, v) for p, v in _flatten(record) if re.search(r"handicap|accessib|wheelchair|(?:^|\.)ada", p, re.I)
           and v not in (None, "", [], {})]
    if acc:
        facts["accessible_parking" if any("park" in p.lower() for p, _ in acc) else "accessibility"] = dict(acc)
    am = record.get("amenities") or []
    if am:
        named = [a for a in am if isinstance(a, str)]
        facts["amenities"] = {"ids": [a for a in am if not isinstance(a, str)], "names": named,
                              "accessibility_related": [a for a in named if ACCESS_WORDS.search(a)],
                              "note": None if named else "the kit has amenity ids but no amenity catalog to name them"}
    for fld, out in (("buildingClass", "building_class"), ("propertyType", "property_type"),
                     ("sprinklerType", "sprinklers"), ("buildingRating", "costar_rating")):
        if record.get(fld) is not None:
            facts[out] = decode(ENUM_FIELDS[fld], record[fld])
    ten = (record.get("propertyOffice") or {}).get("tenancyType")
    if ten is not None:
        facts["tenancy"] = decode("TenancyType", ten)
    for fld, out in (("grossBuildingAreaSquareFeet", "gross_area_m2"), ("rentableBuildingAreaSquareFeet", "rentable_area_m2"),
                     ("lotSize", None)):
        if out and record.get(fld):
            facts[out] = round(record[fld] * SQFT_M2, 1)
    if record.get("buildingName"):
        facts["building_name"] = record["buildingName"]
    a = record.get("primaryAddress") or {}
    if a.get("latitude") is not None:
        facts["lat"], facts["lon"] = a["latitude"], a.get("longitude")
    if a:
        facts["address"] = " ".join(str(a[k]) for k in ("streetNumber", "streetDirection", "streetName", "streetType")
                                    if a.get(k)) + (f", {a['city']}, {a.get('state', '')}" if a.get("city") else "")
    if a.get("crossStreetDescription"):
        facts["cross_street"] = a["crossStreetDescription"]
    if record.get("buildingHeightInFeet"):
        facts["height_m"] = round(record["buildingHeightInFeet"] * 0.3048, 1)
    notes = [n.get("text") or n.get("note") or n.get("description") for n in record.get("notes") or []
             if isinstance(n, dict)]
    access = []
    for n in notes:
        for sent in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", n or "")):
            if ACCESS_NOTE.search(sent) and 20 <= len(sent) <= 220 and sent not in access:
                access.append(sent.strip())
    if access:
        facts["access_notes"] = access[:3]
    for c in record.get("contacts") or []:
        num = re.sub(r"\D", "", str((c or {}).get("contactPhoneNumber") or ""))
        if len(num) == 10 and num[3:6] != "555" and not num.startswith("555"):  # 555 numbers are fictional
            facts["phone"] = f"{num[:3]}-{num[3:6]}-{num[6:]}"
            break
    rel = record.get("_related") or {}
    if rel.get("tenants") is not None:
        facts["tenants"] = rel["tenants"]
    if rel.get("listing"):
        facts["listing"] = rel["listing"]
    return facts


ACCESS_NOTE = re.compile(r"\b(entrance|entry|lobby|elevator|lift|ramp|ada\b|accessib|wheelchair|handicap|stair|"
                         r"step-free|level entry|parking)", re.I)

def spoken(facts, name="This building"):
    """the building record as a few short sentences a blind traveller can use. says what the record doesn't know
    instead of guessing."""
    out = []
    yb, yr = facts.get("year_built"), facts.get("year_renovated")
    kind = " ".join(x for x in (f"class {facts['building_class']}" if facts.get("building_class") else "",
                                str(facts.get("property_type") or "").lower()) if x) or "building"
    if yb:
        if facts.get("pre_ada"):
            out.append(f"CoStar's record says {name} is a {kind} building from {yb}, before the ADA, with no "
                       f"renovation since, so expect steps and narrow doorways.")
        elif yr and yr >= ADA_YEAR:
            parts = [x.lower() for x in facts.get("renovated_parts") or [] if x.lower() in ("elevators", "lobby",
                                                                                              "restrooms", "entrance")]
            extra = f", including the {' and '.join(parts)}" if parts else ""
            out.append(f"CoStar's record says {name} is a {kind} building from {yb}, renovated in {yr}{extra}.")
        else:
            out.append(f"CoStar's record says {name} is a {kind} building from {yb}.")
    floors = facts.get("floors")
    lifts = facts.get("passenger_elevators")
    if lifts:
        out.append(f"It has {lifts} passenger elevator{'s' * (lifts != 1)}.")
    elif floors and floors > 1 and "elevators" not in facts:
        out.append(f"It has {floors} floors, and the record doesn't say whether there's an elevator.")
    pk = facts.get("parking") or {}
    if pk.get("total"):
        acc = "" if facts.get("accessible_parking") else "; the record doesn't list accessible spaces"
        out.append(f"There are {pk['total']} parking spaces{acc}.")
    ten = [t for t in facts.get("tenants") or [] if t.get("name")]
    if ten:
        # no floors here: costar counts from the street door, the scan from its lowest level, and the route says
        # where each one really is
        named = [t["name"] for t in ten[:3]]
        out.append("Inside: " + (", ".join(named[:-1]) + " and " + named[-1] if len(named) > 1 else named[0]) + ".")
    if facts.get("access_notes"):
        out.append("The listing notes: " + facts["access_notes"][0])
    if facts.get("cross_street"):
        out.append(f"It's near {facts['cross_street']}.")
    if facts.get("phone"):
        out.append(f"To call ahead, the building's number is {facts['phone']}.")
    return out


FACTS = {
    "year_built": r"year.?built|built.?year|yearbuilt|construct.*year",
    "year_renovated": r"renovat",
    "floors": r"stories|storeys|floors|levels",
    "elevators": r"elevator|lift",
    "accessible_parking": r"(handicap|accessible|(?:^|[^a-z])ada(?:[^a-z]|$)|disab).*(park|stall|space)|(park|stall|space).*(handicap|accessible|(?:^|[^a-z])ada(?:[^a-z]|$)|disab)",
    "parking": r"parking",
    "amenities": r"amenit",
    "tenants": r"tenant|occupant",
    "suites": r"suite|available.?space|space.?avail",
    "building_class": r"(building.?)?class$|buildingclass",
    "property_type": r"(property|building).?(sub.?)?type|propertytype",
    "accessibility": r"(?:^|[^a-z])ada(?:[^a-z]|$)|accessib|wheelchair",
}
REAL_KEYS = ("year_built", "year_renovated", "floors", "elevators", "parking", "accessible_parking", "amenities",
             "tenants", "building_class", "property_type", "gross_area_m2", "lat")


def _guess_facts(record):
    """fallback for kits of unknown shape: match keys by name."""
    flat = list(_flatten(record))
    facts = {}
    for fact, pat in FACTS.items():
        rx = re.compile(pat, re.I)
        if fact in ("parking", "accessibility"):  # accessible-stall keys belong to accessible_parking only
            hits = [(p, v) for p, v in flat if rx.search(p) and not re.search(FACTS["accessible_parking"], p, re.I)]
        else:
            hits = [(p, v) for p, v in flat if rx.search(p)]
        if fact in ("tenants", "suites", "amenities"):
            for top in record:  # lists live under a parent key; take the subtree
                if rx.search(str(top)) and isinstance(record[top], (list, dict)):
                    hits = [(top, record[top])]
                    break
        vals = [(p, v) for p, v in hits if v not in (None, "", [], {})]
        if vals:
            facts[fact] = vals[0][1] if len(vals) == 1 or fact not in ("parking", "accessibility") else dict(vals)
    return facts


def _year(v):
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


def accessibility_facts(record):
    """what matters for a blind or wheelchair visitor; never fails on unexpected shapes.
    pre_ada: first occupied before january 26, 1993 (the ada new-construction line) and not renovated since."""
    record = record or {}
    real = _is_real(record)
    facts = real_facts(record) if real else _guess_facts(record)
    yb, yr = _year(facts.get("year_built")), _year(facts.get("year_renovated"))
    facts["pre_ada"] = None if yb is None else (yb < ADA_YEAR and not (yr is not None and yr >= ADA_YEAR))
    paths = [p for p, _ in _flatten({k: v for k, v in record.items() if k != "_related"})]
    keys = REAL_KEYS if real else tuple(FACTS)
    return {"facts": facts, "raw_keys_seen": paths[:200], "missing": [f for f in keys if f not in facts]}


def summary(sid):
    r = lookup(sid)
    if r.get("error"):
        return r
    return {**{k: r[k] for k in ("sid", "key", "kind", "file")}, **accessibility_facts(r["record"])}
