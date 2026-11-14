"""caretaker app endpoints: pair by code, send a trip remotely, watch the traveller live."""
from fastapi.testclient import TestClient

from nudge import hub
from nudge.server import app

c = TestClient(app)


def test_caretaker_sends_a_trip_and_watches_live(tmp_path):
    hub.reset(tmp_path / "hub.json")
    reg = c.post("/api/traveller/register", json={"name": "Sam", "traveller_id": "t-app"}).json()

    # caretaker pairs with the code shown on the traveller's phone
    p = c.post("/api/caretaker/pair", json={"code": reg["code"]}).json()
    assert p == {"traveller_id": "t-app", "name": "Sam"}
    assert c.post("/api/caretaker/pair", json={"code": "000000" if reg["code"] != "000000" else "111111"}).status_code == 404

    # caretaker sends a trip; the phone on the traveller's back picks it up
    assert c.get("/api/traveller/t-app/trip").json() == {"trip": None}
    sent = c.post("/api/traveller/t-app/trip", json={"sid": "synthetic", "to": "Office", "profile": "blind"}).json()
    assert sent["ok"] and sent["trip"]["to"] == "Office"
    trip = c.get("/api/traveller/t-app/trip").json()["trip"]
    assert trip["sid"] == "synthetic" and trip["profile"] == "blind"

    s = c.post("/api/nav/start", json={**{k: trip[k] for k in ("sid", "to", "profile")},
                                       "start": "entrance", "traveller": "t-app"}).json()
    assert c.post("/api/traveller/t-app/trip/ack").json() == {"ok": True}
    assert c.get("/api/traveller/t-app/trip").json() == {"trip": None}

    # the caretaker's map gets a live position and the route
    st = c.get("/api/traveller/t-app/status").json()
    assert st["session"] == s["session"] and st["state"] == "walking"
    assert st["position"]["floor"] == "f1" and st["position"]["sid"] == "synthetic"
    assert len(st["route"]["points"]) >= 2


def test_unknown_traveller_is_404(tmp_path):
    hub.reset(tmp_path / "hub.json")
    assert c.post("/api/traveller/nobody/trip", json={"sid": "synthetic", "to": "Office"}).status_code == 404
    assert c.get("/api/traveller/nobody/trip").status_code == 404
    assert c.post("/api/traveller/nobody/trip/ack").status_code == 404
