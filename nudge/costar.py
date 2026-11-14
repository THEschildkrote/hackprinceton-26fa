"""the costar kit: a password-protected zip of jsonl (and csv) records plus docs (index.md).

lookup(sid) joins a matterport model to its record through reference/models_index.csv
(costar_property_id for commercial, homes_listing_id for residential). the kit is extracted once
under cache/costar_kit/ (gitignored) and indexed by id -> (file, byte offset), so a lookup is one seek.

env (.env): COSTAR_KIT_ZIP (a downloaded zip) or COSTAR_KIT_URL (downloaded once), COSTAR_ZIP_PASSWORD.
"""
import csv
import json
import re
import zipfile

import requests

from .mp import ROOT, secret

KIT_DIR = ROOT / "cache" / "costar_kit"
AES_METHOD = 99  # winzip aes; python's zipfile only does zipcrypto
ID_KEY = re.compile(r"(property.?id|listing.?id|^id$|costar.?id|propertyid|listingid)", re.I)


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
            raise KitError(f"COSTAR_KIT_ZIP points to a file that doesn't exist")
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
                dest = P["docs"] / name
                kind = "docs"
            elif low.endswith((".jsonl", ".json", ".ndjson", ".csv")):
                dest = P["data"] / name
                kind = "data"
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


def _kind(name):
    low = name.lower()
    if any(w in low for w in ("residential", "homes", "listing")) and "commercial" not in low:
        return "residential"
    return "commercial"


def _ids(rec):
    """top-level and one-level-nested id values worth indexing."""
    out = []
    for k, v in rec.items():
        if isinstance(v, (str, int)) and ID_KEY.search(k):
            out.append(str(v))
        elif isinstance(v, dict):
            out += [str(x) for kk, x in v.items() if isinstance(x, (str, int)) and ID_KEY.search(kk)]
    return out


def build_index(refresh=False):
    """id -> [file, byte offset, kind]. jsonl lines are records; csv rows keep their header."""
    P = _paths()
    if P["index"].exists() and not refresh:
        return json.loads(P["index"].read_text(encoding="utf-8"))
    if not P["data"].exists() or not any(P["data"].rglob("*")):
        extract()
    index = {}
    for f in sorted(P["data"].rglob("*")):
        if not f.is_file():
            continue
        rel, kind = str(f.relative_to(P["data"])), _kind(f.name)
        with open(f, "rb") as fh:
            if f.suffix.lower() == ".csv":
                header = next(csv.reader([fh.readline().decode("utf-8", "replace")]), [])
                cols = [i for i, h in enumerate(header) if ID_KEY.search(h)]
                while True:
                    off = fh.tell()
                    line = fh.readline()
                    if not line:
                        break
                    row = next(csv.reader([line.decode("utf-8", "replace")]), [])
                    for i in cols:
                        if i < len(row) and row[i]:
                            index.setdefault(row[i], [rel, off, kind])
                continue
            while True:
                off = fh.tell()
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for v in _ids(rec):
                    index.setdefault(v, [rel, off, kind])
    KIT_DIR.mkdir(parents=True, exist_ok=True)
    P["index"].write_text(json.dumps(index), encoding="utf-8")
    return index


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


def lookup(sid):
    """{sid, key, kind, file, record} or {sid, error}."""
    if sid.startswith("synthetic"):
        return {"sid": sid, "key": "synthetic", "kind": "commercial", "file": None, "record": SYNTHETIC}
    row = _index_row(sid)
    if not row:
        return {"sid": sid, "error": "not in models_index.csv"}
    key = row.get("costar_property_id") or row.get("homes_listing_id")
    if not key:
        return {"sid": sid, "error": "this model has no CoStar property id or Homes listing id"}
    try:
        idx = build_index()
    except KitError as ex:
        return {"sid": sid, "key": key, "error": str(ex)}
    hit = idx.get(str(key))
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


def accessibility_facts(record):
    """pick out what matters for a blind or wheelchair visitor; never fails on unexpected shapes."""
    record = record or {}
    flat = list(_flatten(record))
    paths = [p for p, _ in flat]
    facts = {}
    for fact, pat in FACTS.items():
        rx = re.compile(pat, re.I)
        if fact in ("parking", "accessibility"):  # accessible-stall keys belong to accessible_parking only
            hits = [(p, v) for p, v in flat if rx.search(p) and not re.search(FACTS["accessible_parking"], p, re.I)]
        else:
            hits = [(p, v) for p, v in flat if rx.search(p)]
        if fact in ("tenants", "suites", "amenities"):
            # lists live under a parent key; take the subtree
            for top in record:
                if rx.search(str(top)) and isinstance(record[top], (list, dict)):
                    hits = [(top, record[top])]
                    break
        vals = [(p, v) for p, v in hits if v not in (None, "", [], {})]
        if vals:
            facts[fact] = vals[0][1] if len(vals) == 1 or fact not in ("parking", "accessibility") else dict(vals)
    yb = facts.get("year_built")
    try:
        facts["pre_ada"] = int(str(yb)[:4]) < 1993 if yb is not None else None  # first occupancy after jan 26 1993
    except ValueError:
        facts["pre_ada"] = None
    return {"facts": facts, "raw_keys_seen": paths[:200], "missing": [f for f in FACTS if f not in facts]}


def summary(sid):
    r = lookup(sid)
    if r.get("error"):
        return r
    return {**{k: r[k] for k in ("sid", "key", "kind", "file")}, **accessibility_facts(r["record"])}
