"""stall detection: one traveller stopping, backing up or shuffling gets asked; several make a stretch cost more."""
import time

import pytest
from fastapi.testclient import TestClient

from nudge import stalls, walkgraph
from nudge.navigator import Navigator
from nudge.pdr import Tracker
from nudge.router import PROFILES, edge_cost
from tests.synthetic import building

GOAL = "Conference Room"


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    from nudge import hub
    monkeypatch.setattr(stalls, "_FILE", tmp_path / "stalls.json")
    stalls.reset()
    hub.reset(tmp_path / "hub.json")
    yield
    stalls.reset()
    hub.reset()


def walker(start="p_K3"):
    g = walkgraph.build(building())
    s = g.nodes[start]
    nav = Navigator(g, GOAL, "blind")
    nav.start(s.x, s.y, s.floor, 0.0)
    return g, nav, Tracker(nav)


def walk(tr, session, t, seconds, steps_per_s=2):
    """steps at walking pace, one update a second; returns the clock and anything detected."""
    seen = []
    for _ in range(seconds):
        t += 1.0
        tr.step(steps_per_s, None, now=t)
        s = stalls.detect(session, tr, steps_per_s, now=t)
        if s:
            seen.append(s)
    return t, seen


def test_standing_still_mid_route_asks_once():
    g, nav, tr = walker()
    assert nav.route.length > 10
    t, seen = walk(tr, "s1", 0.0, 4)
    assert not seen and tr.along > stalls.EDGE_GAP_M
    asked = []
    for _ in range(12):   # heading-only updates while they stand there
        t += 1.0
        tr.step(0, None, now=t)
        s = stalls.detect("s1", tr, 0, now=t)
        if s:
            asked.append((t, s))
    assert len(asked) == 1, asked                      # once per stop, not every second
    when, s = asked[0]
    assert s["kind"] == "stop" and when - 4.0 >= stalls.STOP_S
    a, b = s["edge"]
    assert g.edge(a, b) is not None and a in nav.route.nodes and b in nav.route.nodes


def test_backing_up_and_shuffling():
    _, _, tr = walker()
    t, _ = walk(tr, "s2", 0.0, 5)
    tr.along -= stalls.BACK_M + 0.2                     # the tracker retraced: they turned round
    assert stalls.detect("s2", tr, 1, now=t + 1)["kind"] == "backup"

    _, _, tr = walker()
    t, _ = walk(tr, "s3", 0.0, 5)
    hit = None
    for _ in range(int(stalls.SHUFFLE_S)):              # stepping, but going nowhere along the route
        t += 1.0
        hit = hit or stalls.detect("s3", tr, 3, now=t)
    assert hit and hit["kind"] == "shuffle"


def test_no_question_while_waiting_for_a_lift_or_at_the_start():
    _, _, tr = walker()
    for k in range(12):                                 # never walked: standing at the start is not a stall
        assert stalls.detect("s4", tr, 0, now=float(k)) is None
    t, _ = walk(tr, "s5", 0.0, 4)
    tr.pending = [5, "elevator", t, 0]
    for _ in range(12):
        t += 1.0
        assert stalls.detect("s5", tr, 0, now=t) is None


def test_two_travellers_make_a_stretch_cost_more_and_keep_going_takes_one_back():
    g, nav, tr = walker()
    edge = tuple(sorted((nav.route.nodes[1], nav.route.nodes[2])))
    stall = {"kind": "stop", "edge": edge, "x": 0.0, "y": 0.0, "floor": "f1"}
    stalls.record("synthetic", stall, "ann")
    assert stalls.active("synthetic")[0]["penalised"] is False        # one person stopping proves nothing
    stalls.record("synthetic", stall, "ben")
    live = stalls.active("synthetic")
    assert live[0]["travellers"] == 2 and live[0]["penalised"]
    e = g.edge(*edge)
    before = edge_cost(e, PROFILES["blind"])
    assert stalls.apply(g, "synthetic") == {edge} and "stall" in e.tags
    assert edge_cost(e, PROFILES["blind"]) == before + PROFILES["blind"].penalty["stall"]
    stalls.resolve("synthetic", "ben", "clear")                        # "keep going": nothing was there
    assert not stalls.active("synthetic")[0]["penalised"]
    assert stalls.apply(g, "synthetic") == set() and "stall" not in e.tags
    stalls.resolve("synthetic", "ann", "obstacle")                     # one person saying so is enough
    assert stalls.active("synthetic")[0]["penalised"]


def test_stalls_expire(monkeypatch):
    g, nav, _ = walker()
    edge = tuple(sorted((nav.route.nodes[1], nav.route.nodes[2])))
    rec = stalls.record("synthetic", {"kind": "stop", "edge": edge, "x": 0, "y": 0, "floor": "f1"}, "ann")
    stalls.resolve("synthetic", "ann", "obstacle")
    assert stalls.active("synthetic", now=rec["t"] + stalls.TTL_S - 1)
    assert stalls.active("synthetic", now=rec["t"] + stalls.TTL_S + 1) == []


def test_over_http_the_step_asks_and_the_answer_counts(monkeypatch):
    from nudge.server import app
    c = TestClient(app)
    monkeypatch.setattr(stalls, "STOP_S", 0.3)
    g = walkgraph.build(building())
    s0 = g.nodes["p_K3"]
    r = c.post("/api/nav/start", json={"sid": "synthetic", "to": GOAL, "profile": "blind", "x": s0.x, "y": s0.y,
                                       "floor": s0.floor, "heading": 0.0, "traveller": "t-stall"}).json()
    session = r["session"]
    c.post("/api/nav/calibrate", json={"session": session, "heading_deg": 0.0})
    for _ in range(4):
        c.post("/api/nav/step", json={"session": session, "steps": 2})
    c.post("/api/nav/step", json={"session": session, "steps": 0})
    time.sleep(0.4)
    out = c.post("/api/nav/step", json={"session": session, "steps": 0}).json()
    assert out["ask"] == "stall" and stalls.QUESTION in out["say"]
    assert [x["travellers"] for x in c.get("/api/ops/snapshot?events=0").json()["stalls"]["synthetic"]] == [1]
    a = c.post("/api/voice/turn", json={"traveller": "t-stall", "text": "keep going", "session": session}).json()
    assert a["say"].startswith("Okay, carry on.")
    assert stalls.active("synthetic") == []                            # nothing was there: it doesn't count


def test_saying_what_is_there_hands_it_to_the_agent(monkeypatch):
    from nudge.server import app
    c = TestClient(app)
    monkeypatch.setattr(stalls, "STOP_S", 0.3)
    g = walkgraph.build(building())
    s0 = g.nodes["p_K3"]
    session = c.post("/api/nav/start", json={"sid": "synthetic", "to": GOAL, "profile": "blind", "x": s0.x,
                                             "y": s0.y, "floor": s0.floor, "heading": 0.0,
                                             "traveller": "t-cart"}).json()["session"]
    c.post("/api/nav/calibrate", json={"session": session, "heading_deg": 0.0})
    for _ in range(4):
        c.post("/api/nav/step", json={"session": session, "steps": 2})
    c.post("/api/nav/step", json={"session": session, "steps": 0})
    time.sleep(0.4)
    assert c.post("/api/nav/step", json={"session": session, "steps": 0}).json()["ask"] == "stall"
    # agent off: the deterministic way around stands in for the model
    a = c.post("/api/voice/turn", json={"traveller": "t-cart", "text": "there's a cleaning cart across the hall",
                                        "session": session, "agent": False}).json()
    assert a["say"].startswith("Okay, stay where you are.")
    live = stalls.active("synthetic")
    assert live and live[0]["obstacle"] and live[0]["penalised"]     # the next traveller routes around it
