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
