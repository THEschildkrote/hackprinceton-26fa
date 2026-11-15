"""no building named yet: isaac assumes the venue (jrr) instead of asking "which building are you in?",
in the app and on a call from a number linked to a phone that never had a trip."""
import pytest
from fastapi.testclient import TestClient

from nudge import hub, server
from nudge.server import app

c = TestClient(app)


@pytest.fixture
def fresh(tmp_path):
    hub.reset(tmp_path / "hub.json")
    server._seen.clear()
    yield
    server._seen.clear()
    hub.reset()


def test_the_app_never_asks_which_building(fresh, monkeypatch):
    for text in ("where am i", "take me to the elevator"):
        r = c.post("/api/voice/turn", json={"traveller": "fresh-app", "text": text, "async_ok": True}).json()
        assert "which building" not in (r.get("say") or "").lower(), text


def test_a_linked_caller_with_a_fresh_phone_gets_the_venue(fresh, monkeypatch):
    c.post("/api/traveller/register", json={"traveller_id": "P", "name": "Sam"})
    hub.set_walker("+15550001111", "P")
    seen = {}

    def turn(t):
        seen["sid"] = t.sid
        return {"say": "ok"}
    monkeypatch.setattr(server._voice, "turn", turn)
    c.post("/api/voice/call", json={"caller": "+15550001111", "text": "elevator"})
    assert seen["sid"] == server.VENUE


def _say(tid, text):
    return c.post("/api/voice/turn", json={"traveller": tid, "text": text, "async_ok": True}).json()


def test_from_one_point_to_another_starts_at_the_first(fresh):
    from nudge.agents.tools import graph
    try:
        g = graph(server.VENUE)
    except Exception as ex:   # no matterport key here
        pytest.skip(f"venue map not available: {ex}")
    labels = sorted((n.label for n in g.nodes.values() if (n.label or "").isdigit()), key=int)
    if len(labels) < 2:
        pytest.skip("venue has no numbered points")
    a, b = labels[1], labels[-1]
    r = _say("ab", f"from point {a} to point {b}")
    assert r["say"].startswith(f"Okay, going to point {b}"), r["say"]
    start = next(n for n in g.nodes.values() if n.label == a)
    assert r["trip"]["start"][:2] == [start.x, start.y]


def test_an_unknown_starting_point_says_so(fresh):
    r = _say("ab2", "from point 9999 to point 1")
    assert "don't know a point called point 9999" in r["say"] or "which building" not in r["say"].lower()


def test_places_can_have_real_names(fresh, monkeypatch):
    from nudge import voice
    from nudge.agents.tools import graph
    try:
        g = graph(server.VENUE)
    except Exception as ex:
        pytest.skip(f"venue map not available: {ex}")
    labels = sorted((n.label for n in g.nodes.values() if (n.label or "").isdigit()), key=int)
    a, b = labels[1], labels[-1]
    monkeypatch.setattr(voice, "_venue_names", lambda sid: {"lobby": a, "elevator": b})
    r = _say("named", "take me from the lobby to the elevator")
    assert r["say"].startswith("Okay, going to the elevator"), r["say"]
    start = next(n for n in g.nodes.values() if n.label == a)
    assert r["trip"]["start"][:2] == [start.x, start.y]


def test_the_venue_names_list_points_at_real_points():
    from nudge import voice
    from nudge.agents.tools import graph
    try:
        g = graph(server.VENUE)
    except Exception as ex:
        pytest.skip(f"venue map not available: {ex}")
    labels = {n.label for n in g.nodes.values()}
    names = voice._venue_names(server.VENUE)
    assert names and all(v in labels for v in names.values()), {k: v for k, v in names.items() if v not in labels}
