"""the real costar kit, when it has been downloaded and extracted (skipped otherwise)."""
import pytest

from nudge import costar

KIT = costar.KIT_DIR
pytestmark = pytest.mark.skipif(not any(KIT.glob("data/**/commercial-property.jsonl")),
                                reason="real CoStar kit not extracted under cache/costar_kit")


def test_real_join_and_facts_for_1255_s_atlantic():
    r = costar.lookup("MgGNVWsyXtH")
    assert not r.get("error"), r
    assert r["kind"] == "commercial" and r["key"] == "10282061" and r["file"] == "commercial-property.jsonl"
    f = costar.accessibility_facts(r["record"])["facts"]
    assert f["year_built"] == 1917 and f["floors"] == 2 and f["pre_ada"] is True
    assert f["parking"]["total"] == 8 and f["parking"]["by_type"][0]["type"] == "Surface"
    assert f["building_class"] == "B" and f["property_type"] == "Office"
    assert any(t["name"] != "unknown tenant" for t in f["tenants"])


def test_enums_decode_from_the_kit_schema():
    assert costar.decode("BuildingClass", 1) == "A"
    assert costar.decode("ConstructionEventType", 2) == "ConstructionComplete"
    assert costar.decode("ConstructionEventType", 4) == "RenovationFinished"
    assert costar.decode("ParkingType", 1) == "Surface"


def test_index_is_versioned_and_reused():
    idx = costar.build_index()
    assert idx["version"] == costar.INDEX_VERSION and idx["join"]["commercial"]


def test_renovations_use_the_kits_dates_and_name_what_changed():
    rec = {"constructionEvents": [{"eventType": 1, "year": 1960}],
           "renovations": [{"startDate": "2003-02-01", "endDate": "2004-06-30", "renovationImprovementTypes": []}]}
    f = costar.accessibility_facts(rec)["facts"]
    assert f["year_renovated"] == 2004 and f["pre_ada"] is False


def test_the_record_is_spoken_and_says_what_it_doesnt_know():
    facts = {"year_built": 1917, "pre_ada": True, "building_class": "B", "property_type": "Office", "floors": 2,
             "parking": {"total": 8}, "tenants": [{"name": "DJF Insurance", "floor": "FirstFloor"}]}
    lines = costar.spoken(facts, "1255 S Atlantic")
    assert "before the ADA" in lines[0] and "class B office" in lines[0]
    assert any("doesn't say whether there's an elevator" in ln for ln in lines)
    assert any("doesn't list accessible spaces" in ln for ln in lines)
    assert any(ln == "Inside: DJF Insurance." for ln in lines)
    assert not any("call ahead" in ln for ln in costar.spoken({**facts, "phone": None}))
