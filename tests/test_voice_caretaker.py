"""a trip the traveller asks for out loud is followed by their caretaker the same way as one the caretaker sent:
the distance left while walking, and the arrival."""
import pytest
from fastapi.testclient import TestClient

from nudge import hub
from nudge.server import app
from tests import belt_maps

c = TestClient(app)


@pytest.fixture(autouse=True)
def _test_maps(monkeypatch):
    """the drawn straight line from tests/belt_maps.py loads like any building, for these tests only."""
    from nudge import fetch
    real = fetch.load
    monkeypatch.setattr(fetch, "load",
                        lambda sid, refresh=False: belt_maps.model(sid) if sid in belt_maps.NAMES else real(sid, refresh))


@pytest.fixture
def tid(tmp_path):
    hub.reset(tmp_path / "hub.json")
    code = c.post("/api/traveller/register", json={"traveller_id": "V", "name": "Sam"}).json()["code"]
    assert c.post("/api/caretaker/pair", json={"code": code}).json()["traveller_id"] == "V"
    yield "V"
    hub.reset()


def _walk(session):
    c.post("/api/nav/calibrate", json={"session": session, "heading_deg": 0})
    mid = None
    for i in range(10):
        st = c.post("/api/nav/step", json={"session": session, "steps": 3, "heading_deg": 0, "step_m": 0.7}).json()
        if i == 0:
            mid = c.get("/api/traveller/V/status").json()
        if st.get("done"):
            break
    return mid, c.get("/api/traveller/V/status").json()


def _voice(text):
    return c.post("/api/voice/turn", json={"traveller": "V", "text": text, "async_ok": True, "sid": belt_maps.LINE}).json()


def test_a_voice_trip_shows_the_caretaker_the_distance_left_and_the_arrival(tid):
    r = _voice("take me to the finish")
    assert r["session"]
    mid, end = _walk(r["session"])
    assert mid["state"] == "walking" and mid["goal"] == "Finish"
    assert mid["remaining_m"] and mid["remaining_m"] > 0
    assert end["state"] == "arrived"
    assert "arrive" in [e["type"] for e in end["recent"]]


def test_a_voice_trip_and_a_caretaker_trip_look_the_same_to_the_caretaker(tid):
    s = c.post("/api/nav/start", json={"sid": belt_maps.LINE, "to": "Finish", "profile": "blind",
                                       "traveller": "V"}).json()["session"]
    _, by_app = _walk(s)
    _, by_voice = _walk(_voice("take me to the finish")["session"])
    assert by_app["state"] == by_voice["state"] == "arrived"
    assert by_app["remaining_m"] == by_voice["remaining_m"] == 0.0
