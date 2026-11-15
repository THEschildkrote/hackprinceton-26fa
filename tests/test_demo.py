"""the maya demo: tenant as a destination, the briefing, no dead air on agent turns, sam's on-screen texts."""
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nudge import places, voice, walkgraph
from nudge.mp import ROOT
from tests.synthetic import building

T = "maya-test"
REAL = "MgGNVWsyXtH"
HAVE_REAL = (ROOT / "cache" / "models" / f"{REAL}.json").exists() and (ROOT / "cache" / "costar_kit").exists()


@pytest.fixture
def hub(monkeypatch):
    events = []
    stub = types.SimpleNamespace(publish=lambda tid, ev: events.append((tid, ev)), attach_session=lambda *a: None,
                                 caretakers=lambda tid: [], register_traveller=lambda tid, name=None: "123456")
    monkeypatch.setattr(voice, "_hub", lambda: stub)
    return events


class SlowAgent:
    """an exception agent that takes longer than the traveller should wait."""

    def __init__(self, delay, say="**Take the stairs instead.**"):
        self.delay, self.say = delay, say


def client(monkeypatch, agent=None, timeout=0.3, has_caretaker=None, explode=False):
    g = walkgraph.build(building())
    sessions = {}

    def register(nav, sid, tid=None):
        s = f"s{len(sessions)}"
        sessions[s] = nav
        return s

    def make_exc(nav, g_, txt):
        if explode:
            raise AssertionError("the agent must not be called for this")
        return agent
    mod = types.SimpleNamespace(new_thread=lambda: "t",
                                handle_event=lambda a, th, text: (time.sleep(a.delay), {"say": [a.say], "pending": None})[1])
    monkeypatch.setattr(voice, "_agent_module", lambda kind: mod)
    router = voice.make_router(get_navigator=sessions.get, register_session=register, graph_for=lambda sid: g,
                               exception_factory=make_exc, default_sid="synthetic", agent_timeout=timeout,
                               has_caretaker=has_caretaker)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), router, g


def turn(c, text, **kw):
    r = c.post("/api/voice/turn", json={"traveller": T, "text": text, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def start_and_walk(c, router, steps=6):
    from nudge.sim import walk
    turn(c, "take me to the conference room")
    conv = router.conversations[T]
    for i, (x, y, f, h) in enumerate(walk(conv.nav.g, conv.nav.route, step=0.5)):
        c.post("/api/voice/position", json={"traveller": T, "x": x, "y": y, "floor": f, "heading": h})
        if i >= steps:
            break
    return conv


# a: the tenant is somewhere you can go

def test_tenant_placed_on_its_floor_when_no_suite_matches():
    g = walkgraph.build(building())
    seed = places.seed_from_graph(g, "synthetic", costar={"_related": {"tenants": [
        {"name": "DJF Insurance", "floor": "FirstFloor", "suite": None}]}})
    t = next(p for p in seed["places"] if p["kind"] == "tenant")
    assert t["location_known"] and t["source"] == "costar+guess" and t["confidence"] == 0.6
    assert "insurance" in t["aliases"] and t["floor"] == "f1"
    r = places.resolve(g, "take me to the insurance office", catalog={"places": seed["places"]}) \
        if "catalog" in places.resolve.__code__.co_varnames else None
    assert places.the(t) == "DJF Insurance" and places.the("Kitchen") == "the Kitchen"
    assert places.the("Hallway 2, Floor 1") == "Hallway 2, Floor 1"


@pytest.mark.skipif(not HAVE_REAL, reason="real building not cached")
def test_djf_insurance_resolves_on_the_real_building():
    from nudge.agents.tools import graph
    g = graph(REAL)
    for q in ("take me to DJF Insurance", "the insurance office", "djf"):
        assert places.resolve(g, q)["best"]["name"] == "DJF Insurance", q


def test_plural_for_need_questions():
    assert places.plural("place to get water") == "places to get water"
    assert places.plural("bathroom") == "bathrooms"
    assert places.plural("staircase") == "staircases"


# b: the briefing

def test_briefing_endpoint_generates_short_sentences():
    from nudge.server import app
    r = TestClient(app).get("/api/briefing/synthetic", params={"goal": "Kitchen"})
    assert r.status_code == 200
    b = r.json()
    assert b["sentences"] and all(len(s) < 220 for s in b["sentences"])
    assert "caretaker" in b["sentences"][-1]


@pytest.mark.skipif(not HAVE_REAL, reason="real building not cached")
def test_briefing_on_the_real_building_mentions_age_steps_and_route():
    from nudge import demo
    b = demo.briefing(REAL, "DJF Insurance")
    text = b["text"]
    assert "1917" in text and "steps" in text and "meters" in text and "DJF Insurance" in text


# c: no dead air

def test_agent_turn_answers_at_once_then_falls_back_after_the_timeout(monkeypatch, hub):
    c, router, _ = client(monkeypatch, agent=SlowAgent(delay=3.0), timeout=0.3)
    conv = start_and_walk(c, router)
    r = turn(c, "the elevator is out", async_ok=True)
    assert r["say"] == voice.ACK and r["say_now"] == voice.ACK and r["pending_job"]
    j = c.get(f"/api/voice/job/{r['pending_job']}").json()
    while not j["done"]:
        time.sleep(0.1)
        j = c.get(f"/api/voice/job/{r['pending_job']}").json()
    assert j["fallback"] and "way around" in j["say"] and "say yes" in j["say"]
    assert j["pending"]["name"] == "reroute"
    before = (set(conv.nav.blocked_nodes), set(conv.nav.blocked_edges))
    out = turn(c, "yes")
    assert (set(conv.nav.blocked_nodes), set(conv.nav.blocked_edges)) != before
    assert "New route" in out["say"]
    assert any(ev["type"] == "reroute_approved" and ev.get("reason") for _, ev in hub)


def test_scripted_demo_skips_the_agent_and_answers_in_one_turn(monkeypatch, hub):
    c, router, _ = client(monkeypatch, explode=True)
    start_and_walk(c, router)
    r = turn(c, "this door is locked", async_ok=True, agent=False)
    assert r["pending_job"] is None and r["say"].startswith(voice.ACK) and "say yes" in r["say"]


def test_one_shot_clients_wait_no_longer_than_the_timeout(monkeypatch, hub):
    c, router, _ = client(monkeypatch, agent=SlowAgent(delay=3.0), timeout=0.4)
    start_and_walk(c, router)
    t0 = time.monotonic()
    r = turn(c, "the door ahead is locked")
    assert time.monotonic() - t0 < 2.0 and "way around" in r["say"]


def test_fast_agent_wins_and_markdown_is_never_spoken(monkeypatch, hub):
    c, router, _ = client(monkeypatch, agent=SlowAgent(delay=0.0), timeout=5.0)
    start_and_walk(c, router)
    r = turn(c, "the door is locked")
    assert "Take the stairs instead." in r["say"] and "*" not in r["say"]


# audit fixes

def test_problem_phrase_without_a_trip_asks_where_they_were_going(monkeypatch, hub):
    c, _, _ = client(monkeypatch, explode=True)
    r = turn(c, "the door ahead is locked")
    assert "Where were you going" in r["say"]


def test_place_question_mid_trip_offers_to_switch_without_the_agent(monkeypatch, hub):
    c, router, _ = client(monkeypatch, explode=True)
    conv = start_and_walk(c, router)
    r = turn(c, "where is the kitchen")
    assert "Say yes to go there now" in r["say"] and "Conference Room" in r["say"]
    assert conv.trip["goal"] == "Conference Room"
    turn(c, "yes")
    assert conv.trip["goal"] == "Kitchen"


def test_help_with_nobody_paired_gives_the_code(monkeypatch, hub):
    c, _, _ = client(monkeypatch, has_caretaker=lambda tid: False)
    turn(c, "take me to the kitchen")
    r = turn(c, "I need help")
    assert "No caretaker is paired yet" in r["say"] and "1 2 3 4 5 6" in r["say"]
    c2, _, _ = client(monkeypatch, has_caretaker=lambda tid: True)
    assert "let your caretaker know" in turn(c2, "I need help")["say"]


def test_capped_helper_abandons_a_slow_agent():
    from nudge import server
    r, why = server._capped(lambda: time.sleep(2) or 1, timeout=0.2)
    assert r is None and "longer" in why
    r, why = server._capped(lambda: 7, timeout=1)
    assert r == 7 and why is None


# d: sam's on-screen texts match the imessage rules

def test_caretaker_view_matches_the_imessage_notifier(tmp_path):
    from nudge import demo, hub, photon_bot
    hub.reset(tmp_path / "hub.json")
    hub.register_traveller("view-t", "Maya")
    hub.attach_session("view-t", "s1", "synthetic", "DJF Insurance", "blind")
    view = demo.CaretakerView()
    try:
        t0 = time.time()
        hub.publish("view-t", {"type": "route", "goal": "DJF Insurance", "remaining_m": 44.4, "ts": t0})
        hub.publish("view-t", {"type": "leg", "text": "Walk 19 meters.", "floor": "Floor 2", "ts": t0 + 1})
        hub.publish("view-t", {"type": "leg", "text": "Stairs.", "floor": "Floor 1", "ts": t0 + 5})  # within the gap
        hub.publish("view-t", {"type": "arrive", "text": "You have arrived.", "ts": t0 + 30})
        hub.publish("view-t", {"type": "help", "text": "I need help", "ts": t0 + 31})
        msgs = view.messages("view-t")
        texts = [m["text"] for m in msgs]
        assert texts[0].startswith("Maya started walking to DJF Insurance")
        assert texts[1] == "Maya arrived at DJF Insurance."
        assert texts[2].startswith("Maya asked for help") and msgs[2]["urgent"]
        assert len(texts) == 3  # the floor change came inside the 20 s gap, like the real notifier
        # identical wording to photon_bot's notifier
        n = photon_bot.Notifier.__new__(photon_bot.Notifier)
        n.flagged, n.offroute = {}, {}
        v = photon_bot.Voice("Maya", "they")
        assert n.message("view-t", {"type": "arrive"}, "important", v)[1] == texts[1]
    finally:
        view.unsub()
        hub.reset()
