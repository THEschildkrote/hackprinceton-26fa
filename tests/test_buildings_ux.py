"""the paths a judge touches first: finding the building, asking for a room, and the no-match reply."""
from fastapi.testclient import TestClient

from nudge import buildings, places
from nudge.server import app

c = TestClient(app)


def test_featured_puts_the_demo_building_first_and_hides_test_buildings():
    feat = buildings.featured()
    assert feat[0]["sid"] == buildings.DEMO_SID and feat[0]["address"] == "1255 S Atlantic Blvd"
    assert all(b["sid"] not in buildings.HIDDEN for b in feat)


def test_search_by_street_and_city():
    assert buildings.search("atlantic")[0]["sid"] == buildings.DEMO_SID
    assert buildings.search("1255 atlantic")[0]["sid"] == buildings.DEMO_SID
    houston = buildings.search("houston")
    assert houston and all("houston" in (b["city"] + b["address"]).lower() for b in houston)
    assert buildings.search("synthetic")[0]["sid"] == "synthetic"  # reachable only by the exact id


def test_buildings_endpoint():
    r = c.get("/api/buildings").json()
    assert r["demo"] == buildings.DEMO_SID and r["results"][0]["sid"] == buildings.DEMO_SID


def test_no_match_is_short_and_rooms_only():
    r = places.resolve("synthetic", "take me to the pool")
    assert r["message"].startswith("There's no pool on record here. Closest matches:")
    assert len(r["suggestions"]) <= 4 and "elevator call panel" not in r["message"]
    r = places.resolve("synthetic", "take me to the bathroom")
    assert r["message"].startswith("I don't have a bathroom on record in this building.")


def test_voice_assumes_the_venue_then_uses_the_address():
    # no building named: isaac assumes the venue (jrr) rather than asking; naming an address still switches
    t = {"traveller": "ux-judge"}
    say = lambda text: c.post("/api/voice/turn", json={**t, "text": text}).json()["say"]
    assert "which building" not in say("take me to the kitchen").lower()
    assert say("I'm at 1255 Atlantic").startswith("Okay, 1255 S Atlantic Blvd, Los Angeles.")
    assert "Route ready" in say("take me to the kitchen")
