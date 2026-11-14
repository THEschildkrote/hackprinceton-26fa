"""bring-up orchestration and the costar kit, all offline: mocks stand in for matterport, vexcel and the kit."""
import csv
import json
import os
import struct
import zlib

import pytest

from nudge import bringup, costar, fetch
from nudge.mp import MatterportError, ROOT

SECRET = "sk-sentinel-never-print-0123456789"


# a real zipcrypto archive, written by hand (zipfile can read these but not write them)

def _crc_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        t.append(c)
    return t


CRC = _crc_table()


class _ZipCrypto:
    def __init__(self, pw):
        self.k = [0x12345678, 0x23456789, 0x34567890]
        for ch in pw:
            self._update(ch)

    def _crc(self, old, b):
        return (old >> 8) ^ CRC[(old ^ b) & 0xFF]

    def _update(self, b):
        k0 = self._crc(self.k[0], b)
        k1 = ((self.k[1] + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        k2 = self._crc(self.k[2], k1 >> 24)
        self.k = [k0, k1, k2]

    def encrypt(self, data):
        out = bytearray()
        for b in data:
            t = (self.k[2] | 2) & 0xFFFF
            out.append(b ^ (((t * (t ^ 1)) >> 8) & 0xFF))
            self._update(b)
        return bytes(out)


def write_encrypted_zip(path, files, password):
    """stored (uncompressed) entries, traditional pkware encryption."""
    pw = password.encode()
    local, central, off = b"", b"", 0
    for name, data in files.items():
        nb, crc = name.encode(), zlib.crc32(data) & 0xFFFFFFFF
        enc = _ZipCrypto(pw)
        header = os.urandom(11) + bytes([crc >> 24])
        payload = enc.encrypt(header) + enc.encrypt(data)
        lh = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x1, 0, 0, 0x21, crc, len(payload), len(data), len(nb), 0)
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x1, 0, 0, 0x21, crc, len(payload),
                               len(data), len(nb), 0, 0, 0, 0, 0, off) + nb
        local += lh + nb + payload
        off += len(lh) + len(nb) + len(payload)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(files), len(files), len(central), len(local), 0)
    path.write_bytes(local + central + end)


def index_rows():
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    com = next(r for r in rows if r["costar_property_id"] and r["segment"] == "commercial")
    res = next(r for r in rows if r["homes_listing_id"] and not r["costar_property_id"])
    return com, res


@pytest.fixture
def kit(tmp_path, monkeypatch):
    com, res = index_rows()
    rec = {"propertyId": com["costar_property_id"], "building": {"yearBuilt": 1978, "numberOfStories": 12,
           "elevatorCount": 4, "buildingClass": "A"}, "propertyType": "Office",
           "parking": {"totalSpaces": 300, "handicapSpaces": 8}, "amenities": ["Fitness Center", "Conference Room"],
           "tenants": [{"name": "Dental Partners", "suite": "Suite 400"}]}
    other = {"propertyId": "999999999", "building": {"yearBuilt": 2005}}
    jsonl = (json.dumps(other) + "\n" + json.dumps(rec) + "\n").encode()
    listings = f"listing_id,price,beds,year_built\n111,1,2,1990\n{res['homes_listing_id']},2,3,2001\n".encode()
    docs = b"# CoStar kit\n\n| field | meaning |\n|---|---|\n| propertyId | CoStar property id |\n| `yearBuilt` | year |\n"
    z = tmp_path / "kit.zip"
    write_encrypted_zip(z, {"index.md": docs, "commercial-property/properties.jsonl": jsonl,
                            "residential-listings.csv": listings}, "hunter2")
    monkeypatch.setattr(costar, "KIT_DIR", tmp_path / "kitcache")
    env = {"COSTAR_KIT_ZIP": str(z), "COSTAR_ZIP_PASSWORD": "hunter2"}
    monkeypatch.setattr(costar, "secret", lambda n, required=True: env.get(n))
    return {"zip": z, "com": com, "res": res, "rec": rec}


# costar

def test_kit_decrypts_indexes_and_joins(kit):
    out = costar.extract()
    assert "index.md" in out["docs"] and len(out["data"]) == 2
    assert "propertyId" in costar.field_hints() and "yearBuilt" in costar.field_hints()
    r = costar.lookup(kit["com"]["sid"])
    assert r["kind"] == "commercial" and r["record"]["propertyId"] == kit["com"]["costar_property_id"]
    f = costar.accessibility_facts(r["record"])["facts"]
    assert f["year_built"] == 1978 and f["pre_ada"] is True and f["floors"] == 12 and f["elevators"] == 4
    assert f["accessible_parking"] == 8 and f["tenants"][0]["suite"] == "Suite 400"
    rr = costar.lookup(kit["res"]["sid"])
    assert rr["kind"] == "residential" and rr["record"]["year_built"] == "2001"
    assert (costar.KIT_DIR / "index.json").exists()  # second lookup is a seek, not a scan


def test_kit_wrong_password_is_clear(kit, monkeypatch):
    monkeypatch.setattr(costar, "secret", lambda n, required=True: {"COSTAR_KIT_ZIP": str(kit["zip"]),
                                                                    "COSTAR_ZIP_PASSWORD": "nope"}.get(n))
    with pytest.raises(costar.KitError, match="rejected COSTAR_ZIP_PASSWORD"):
        costar.extract()


def test_kit_missing_config_is_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(costar, "KIT_DIR", tmp_path / "none")
    monkeypatch.setattr(costar, "secret", lambda n, required=True: None)
    com, _ = index_rows()
    r = costar.lookup(com["sid"])
    assert "COSTAR_KIT_URL" in r["error"]


def test_aes_detection():
    class Info:
        compress_type = 99
    assert costar.is_aes(Info())


def test_real_shaped_kit_join_enums_tenants(tmp_path, monkeypatch):
    # same layout as the real kit: join csv, data/data/*.jsonl, data/schemas/enums.json
    com, _ = index_rows()
    pid = 4242
    prop = {"id": pid, "numStoriesAboveGrade": 3, "buildingClass": 1, "propertyType": 5, "coStarBuildingType": 1,
            "constructionEvents": [{"eventType": 2, "year": 1980}, {"eventType": 4, "year": 2005}],
            "parking": [{"parkingType": 1, "totalSpaces": 20}], "amenities": [314],
            "structural": {"elevators": [{"elevatorType": 1, "numElevators": 2, "elevatorState": 1}]},
            "floors": [{"propertyFloorId": 7, "floorName": 1}],
            "primaryAddress": {"latitude": 34.0, "longitude": -118.0, "streetNumber": 1, "streetName": "Main", "city": "LA"}}
    occ = [{"id": 1, "propertyId": pid, "tenantLocationId": 9, "toDate": None,
            "spaces": [{"suite": "200", "propertyFloorId": 7, "squareFeetOccupied": 1000}]},
           {"id": 2, "propertyId": pid, "tenantLocationId": 9, "toDate": "2010-01-01"}]
    comp = {"id": 9, "name": "Clinic LA", "profile": {"companyName": "Clinic", "industryType": 0}}
    enums = {"BuildingClass": {"1": "A"}, "PropertyType": {"5": "Office"}, "ParkingType": {"1": "Surface"},
             "ConstructionEventType": {"2": "ConstructionComplete", "4": "RenovationFinished"},
             "ElevatorType": {"1": "Passenger"}, "FloorName": {"1": "FirstFloor"}, "IndustryType": {"0": "Unknown"}}
    jl = lambda rows: "\n".join(json.dumps(r) for r in rows).encode() + b"\n"
    z = tmp_path / "kit.zip"
    write_encrypted_zip(z, {
        "commercial-properties.csv": f"PropertyId,MatterportModelId,Address\n{pid},{com['sid']},1 Main\n".encode(),
        "data/commercial-property.jsonl": jl([prop]), "data/location-occupancy.jsonl": jl(occ),
        "data/company-detail.jsonl": jl([comp]), "schemas/enums.json": json.dumps(enums).encode(),
        "index.md": b"# kit\n"}, "pw")
    monkeypatch.setattr(costar, "KIT_DIR", tmp_path / "kc")
    monkeypatch.setattr(costar, "secret", lambda n, required=True: {"COSTAR_KIT_ZIP": str(z), "COSTAR_ZIP_PASSWORD": "pw"}.get(n))
    r = costar.lookup(com["sid"])
    assert r["key"] == str(pid) and r["file"] == "commercial-property.jsonl"  # the join csv wins over models_index
    f = costar.accessibility_facts(r["record"])["facts"]
    assert f["year_built"] == 1980 and f["year_renovated"] == 2005 and f["pre_ada"] is False  # renovated after 1993
    assert f["building_class"] == "A" and f["parking"]["total"] == 20 and f["passenger_elevators"] == 2
    assert f["tenants"] == [{"name": "Clinic", "industry": "Unknown", "suite": "200", "floor": "FirstFloor",
                             "sqft": 1000, "since": None}]


def test_facts_tolerate_odd_records():
    f = costar.accessibility_facts({"x": None, "ADA_Compliant": "Yes", "Year Built": "1999-01-01"})
    assert f["facts"]["accessibility"] == "Yes" and f["facts"]["pre_ada"] is False
    assert "elevators" in f["missing"]


# bring-up

@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(bringup, "OUT", tmp_path / "bringup")
    return tmp_path / "bringup"


def _georef_site(monkeypatch):
    """the synthetic city block from the outdoor tests stands in for vexcel + osm."""
    from nudge import handoff, outdoor, vexcel
    from nudge.georef import outline, register
    from tests.synthetic_outdoor import Block
    b = Block()
    monkeypatch.setattr(vexcel, "TRANSPORT", b.transport)
    monkeypatch.setattr(outdoor, "OVERPASS_TRANSPORT", b.overpass_transport)
    og = outdoor.build(34.05, -118.25, footprint_lonlat=vexcel.footprint(34.05, -118.25))
    geo = register(outline(b.gi), [b.enu.to_enu(*p) for p in b.footprint_lonlat], enu=b.enu)
    site = handoff.Site("synthetic", b.gi, og, geo)
    site.portals = handoff.entrance_candidates(b.gi, geo, og, "synthetic")
    monkeypatch.setattr(handoff, "load_site", lambda sid, refresh=False: site)
    return site


def test_synthetic_end_to_end_offline(out, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    _georef_site(monkeypatch)
    report, code = bringup.run("synthetic")
    st = {k: v["status"] for k, v in report["steps"].items()}
    assert st == {"keys": "pass", "probe": "skip", "pick": "pass", "fetch": "pass", "calibrate": "pass",
                  "georef": "pass", "costar": "pass", "dossier": "skip"}, report["steps"]
    assert code == 0
    cal = report["steps"]["calibrate"]["data"]
    assert len(cal["views"]) == 4 and all(os.path.exists(p) for p in cal["views"])
    assert cal["suggested_offset_deg"] in (0, None)
    geo = report["steps"]["georef"]["data"]
    assert geo["entrance_candidates"] >= 1 and os.path.exists(geo["overlay"])
    assert (out / "synthetic.json").exists() and report["next"][1].endswith("tour.html?sid=synthetic")
    printed = capsys.readouterr().out + (out / "synthetic.json").read_text()
    assert SECRET not in printed, "key values must never be printed or saved"


def test_keys_step_names_missing_and_suggests_setkey(out, monkeypatch):
    monkeypatch.setattr(bringup, "secret", lambda n, required=True: SECRET if n == "PHOTON_API_KEY" else None)
    status, msg, data = bringup.step_keys(bringup.Ctx("MgGNVWsyXtH"))
    assert status == "fail" and "python -m nudge.setkey MATTERPORT_API_KEY" in msg
    assert SECRET not in msg + json.dumps(data)


class FakeGQL:
    """answers the probe's queries like the model api would."""

    def __init__(self, edge_types=("wall", "wall", "door", "opening", "gap"), fail=False):
        self.edge_types, self.fail = edge_types, fail

    def __call__(self, q, **v):
        if self.fail:
            raise MatterportError("key rejected (HTTP 401)")
        if "rootFolder" in q:
            return {"rootFolder": {"name": "HackPrinceton 2026", "modelCountSummary": 14945}}
        if "__type" in q:
            return {"__type": {"fields": [{"name": f} for f in ("id", "label", "position", "floor", "room",
                                                               "rotation", "skybox")]} if v["n"] == "Location" else None}
        if "edges { type }" in q:
            return {"model": {"floors": [{"edges": [{"type": t} for t in self.edge_types]}]}}
        raise AssertionError(q)


def test_probe_reads_schema_and_suggests_passable(out, monkeypatch):
    import nudge.mp as mp
    monkeypatch.setattr(mp, "graph", FakeGQL())
    status, msg, data = bringup.step_probe(bringup.Ctx("MgGNVWsyXtH"))
    assert status == "pass" and data["model_count"] == 14945
    assert data["pano_floor"] and data["pano_room"] and data["rotation_field"] == "rotation"
    assert data["passable_suggestion"] == ["gap"] and "PASSABLE" in msg


def test_probe_rejected_key_says_how_to_fix(out, monkeypatch):
    import nudge.mp as mp
    monkeypatch.setattr(mp, "graph", FakeGQL(fail=True))
    status, msg, _ = bringup.step_probe(bringup.Ctx("MgGNVWsyXtH"))
    assert status == "fail" and "nudge.setkey MATTERPORT_API_KEY" in msg


def test_pick_prefers_costar_plus_splat(out):
    ctx = bringup.Ctx()
    status, msg, data = bringup.step_pick(ctx)
    assert status == "pass" and ctx.sid == data["top"][0]["sid"]
    assert data["top"][0]["splat"] == "yes" and data["top"][0]["costar_property_id"]


def test_fetch_flags_door_edges_typed_unexpectedly(out, monkeypatch):
    from tests.synthetic import building
    m = building()
    for f in m["floors"]:
        for e in f["edges"]:
            if e["type"] in ("door", "opening"):
                e["type"] = "gap"
    m["_dropped"] = ["pano_room"]
    monkeypatch.setattr(fetch, "load", lambda sid, refresh=False: m)
    status, msg, data = bringup.step_fetch(bringup.Ctx("MgGNVWsyXtH"))
    assert status == "pass" and data["dropped"] == ["pano_room"]
    assert data["unconnected_panos"] or any("bridged" in w for w in data["warnings"])
    assert "add ['gap'] to PASSABLE" in data["fix"]


def test_failures_are_reported_not_raised(out, monkeypatch):
    def boom(sid, refresh=False):
        raise MatterportError("not found")
    monkeypatch.setattr(fetch, "load", boom)
    from nudge import handoff
    monkeypatch.setattr(handoff, "load_site", lambda sid, refresh=False: (_ for _ in ()).throw(RuntimeError("VEXCEL_API_KEY is not set")))
    report, code = bringup.run("MgGNVWsyXtH", ["fetch", "calibrate", "georef", "nope"])
    st = {k: v["status"] for k, v in report["steps"].items()}
    assert st == {"fetch": "fail", "calibrate": "skip", "georef": "fail"} and code == 1
    assert "VEXCEL_API_KEY" in report["steps"]["georef"]["message"]


def test_dossier_step_runs_quick_survey_when_asked(out, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    from nudge.agents import dossier
    calls = {}

    def fake_run(sid, focus=None, goal=None, on_event=print, **kw):
        calls.update(sid=sid, focus=focus, goal=goal)
        d = tmp_path / "dossier"
        d.mkdir()
        (d / "annotations.json").write_text("{}")
        return d
    monkeypatch.setattr(dossier, "run", fake_run)
    report, _ = bringup.run("synthetic", ["fetch", "dossier"], dossier=True)
    s = report["steps"]["dossier"]
    assert s["status"] == "pass" and calls["sid"] == "synthetic" and calls["focus"] and calls["goal"]
