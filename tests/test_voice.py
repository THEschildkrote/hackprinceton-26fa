"""the traveller's voice: deterministic intents first, agents for the rest, caretaker events to the hub."""
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nudge import voice, walkgraph
from nudge.agents.exception import create_exception_agent
from nudge.agents.explorer import create_explorer_agent
from tests.synthetic import building
from tests.test_exception import script, scripted

T = "+15555550123"


@pytest.fixture
def hub(monkeypatch):
    events, attached = [], []
    stub = types.SimpleNamespace(publish=lambda tid, ev: events.append((tid, ev)),
                                 attach_session=lambda *a: attached.append(a))
    monkeypatch.setattr(voice, "_hub", lambda: stub)
    return types.SimpleNamespace(events=events, attached=attached)


def client(exception_model=None, explorer_model=None):
    g = walkgraph.build(building())
    sessions = {}

    def register(nav, sid, tid=None):
        s = f"s{len(sessions)}"
        sessions[s] = nav
        return s

    router = voice.make_router(
        get_navigator=sessions.get, register_session=register, graph_for=lambda sid: g,
        exception_factory=lambda nav, g_, txt: create_exception_agent(nav, g_, txt, model=exception_model),
        explorer_factory=lambda nav, g_, sid: create_explorer_agent(nav, g_, sid="synthetic", model=explorer_model),
        default_sid="synthetic")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), router, g


def say(c, text, **kw):
    r = c.post("/api/voice/turn", json={"traveller": T, "text": text, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_start_a_trip_by_voice_first_step_in_meters(hub):
    c, router, _ = client()
    out = say(c, "Take me to the conference room")
    assert out["say"].startswith("Okay, going to the Conference Room.")
    assert "meter" in out["say"] and "feet" not in out["say"]
    assert out["session"] and out["device"]["line"].startswith("N,")
    assert any(ev["type"] == "trip_start" for _, ev in hub.events)
    assert hub.attached and hub.attached[0][0] == T
    assert "meter" in say(c, "how far")["say"]
    assert say(c, "what's next")["say"]


def test_where_am_i(hub):
    c, _, g = client()
    k = g.nodes["p_K2"]
    out = say(c, "where am I", x=k.x, y=k.y, floor=k.floor)
    assert "Kitchen" in out["say"]


def test_ambiguous_place_then_answer(hub):
    c, router, _ = client()
    out = say(c, "how do I get to the elevator")
    assert out["choice"] and out["say"].endswith("Which one?"), out
    out = say(c, "floor 2")
    assert out["say"].startswith("Okay, going to the Elevator") and "Floor 2" in out["say"], out
    conv = router.conversations[T]
    assert conv.choice is None and conv.nav is not None


def test_help_alerts_the_caretaker(hub):
    c, _, g = client()
    lb = g.nodes["p_L2"]
    out = say(c, "I need help", x=lb.x, y=lb.y, floor=lb.floor)
    assert "caretaker" in out["say"] and "Lobby" in out["say"]
    helps = [ev for _, ev in hub.events if ev["type"] == "help"]
    assert helps and "Lobby" in helps[0]["where"]
    say(c, "please call my caretaker")
    assert any(ev["type"] == "call_request" for _, ev in hub.events)


def test_problem_goes_to_exception_agent_and_yes_approves(hub):
    c, router, g = client(exception_model=script())
    say(c, "take me to the conference room")
    conv = router.conversations[T]
    o4 = g.nodes["p_O4"]
    out = say(c, "the elevator is out of service", x=o4.x, y=o4.y, floor=o4.floor)
    assert out["pending"] and out["pending"]["name"] == "commit_reroute"
    assert "Say yes" in out["say"] and "p_E1" not in conv.nav.blocked_nodes
    out = say(c, "yes")
    assert "p_E1" in conv.nav.blocked_nodes and "p_S1" in conv.nav.route.nodes
    assert out["pending"] is None
    assert any(ev["type"] == "reroute_approved" for _, ev in hub.events)


def test_off_the_map_goes_to_the_explorer(hub):
    model = scripted(("explorer_status", {}, ""),
                     "You're off the map. Turn around and walk back 2 meters to the lobby.")
    c, router, _ = client(explorer_model=model)
    say(c, "take me to the kitchen")
    conv = router.conversations[T]
    conv.nav.unmapped = True
    out = say(c, "I don't know where I am")
    assert "off the map" in out["say"] and "explorer" in conv.agents


def test_position_reports_speak_and_feed_the_caretaker(hub):
    from nudge.sim import walk
    c, router, g = client()
    say(c, "take me to the office")
    conv = router.conversations[T]
    spoken = []
    for x, y, f, h in walk(g, conv.nav.route, step=0.5):
        r = c.post("/api/voice/position", json={"traveller": T, "x": x, "y": y, "floor": f, "heading": h}).json()
        if r["say"]:
            spoken.append(r["say"])
        if conv.nav.done:
            break
    assert any("arrived" in s for s in spoken)
    assert any(ev["type"] == "arrive" for _, ev in hub.events)


def test_elevenlabs_tool_definition():
    c, _, _ = client()
    tools = c.get("/api/voice/elevenlabs/tools", params={"base": "https://example.test"}).json()["tools"]
    t = tools[0]
    assert t["type"] == "webhook" and t["api_schema"]["url"] == "https://example.test/api/voice/call"
    assert t["api_schema"]["method"] == "POST"


def test_works_without_a_hub(monkeypatch):
    monkeypatch.setattr(voice, "_hub", lambda: None)
    c, _, _ = client()
    assert "Okay, going to" in say(c, "take me to the kitchen")["say"]
    assert "caretaker" in say(c, "I need help")["say"]
