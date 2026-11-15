"""the walk-anywhere demo maps, driven the way the phone drives them: start at the entrance, calibrate, step.

the walker here is a compass heading and a step count, which is all the real phone sends. it either obeys
(turns toward where the reply says to walk) or holds a heading we give it.
"""
import math

import pytest
from fastapi.testclient import TestClient

from nudge import demomaps, server, walkgraph
from nudge.band import Engine
from nudge.navigator import Navigator
from nudge.pdr import Tracker

c = TestClient(server.app)
CAL = 200.0   # what the compass happens to read when they face the first leg; any number must do


def walk(sid, hold, n=60):
    """hold(i) -> a compass heading to keep on step i, or None to obey. returns the whole walk."""
    # traveller "replay" keeps the stall detector out of it: a scripted walker has no real pauses to judge
    r = c.post("/api/nav/start", json={"sid": sid, "to": demomaps.GOAL, "start": "entrance", "traveller": "replay"})
    assert r.status_code == 200, r.text
    d = r.json()
    s = d["session"]
    assert c.post("/api/nav/calibrate", json={"session": s, "heading_deg": CAL}).json()["ok"]
    out = {"start": d, "say": [e["text"] for e in d["events"] if e.get("text")], "cues": [], "pos": [],
           "bearing": [], "done": False, "reroutes": 0}
    compass = CAL
    for i in range(n):
        held = hold(i)
        if held is not None:
            compass = held
        o = c.post("/api/nav/step", json={"session": s, "steps": 1, "heading_deg": compass, "step_m": 0.7}).json()
        out["say"] += o["say"]
        out["cues"] += [e["cue"] for e in o["events"] if e.get("cue")]
        out["pos"].append((o["position"]["x"], o["position"]["y"]))
        out["bearing"].append(o["bearing_to_next_deg"])
        out["reroutes"] = o["state"]["reroutes"]
        if o["done"]:
            out["done"] = True
            break
        if held is None and o["bearing_to_next_deg"] is not None:
            compass = (compass - o["bearing_to_next_deg"]) % 360   # + is a left turn; compass counts clockwise
    return out


def said(w, text):
    return any(text.lower() in s.lower() for s in w["say"])


# --- finding them ---------------------------------------------------------------------------------

def test_typing_demo_finds_both_and_the_featured_list_never_shows_them():
    got = [b["sid"] for b in c.get("/api/buildings", params={"q": "demo"}).json()["results"]]
    assert got == [demomaps.LINE, demomaps.CORNER]
    assert [b["sid"] for b in c.get("/api/buildings", params={"q": "demo corner"}).json()["results"]] == [demomaps.CORNER]
    assert [b["sid"] for b in c.get("/api/buildings", params={"q": demomaps.LINE}).json()["results"]] == [demomaps.LINE]
    featured = [b["sid"] for b in c.get("/api/buildings").json()["results"]]
    assert demomaps.LINE not in featured and demomaps.CORNER not in featured


@pytest.mark.parametrize("sid", [demomaps.LINE, demomaps.CORNER])
def test_the_caretaker_app_can_list_the_finish_and_draw_the_floor(sid):
    names = [p["name"] for p in c.get(f"/api/places/{sid}").json()["places"]]
    assert demomaps.GOAL in names
    g = c.get(f"/api/graph/{sid}")
    assert g.status_code == 200
    assert g.json()["floors"][0]["walls"]


# --- the straight line ----------------------------------------------------------------------------

def test_line_starts_where_the_traveller_stands_and_is_the_length_it_says():
    w = walk(demomaps.LINE, lambda i: None)
    assert said(w, "Route ready: 10 meters, 0 turns")
    x, y = w["pos"][0]
    assert (x, y) == pytest.approx((3.5, 1.7), abs=0.05)   # one 0.7 m step from the start at (3.5, 1)


def test_line_walked_straight_arrives_with_no_alert():
    w = walk(demomaps.LINE, lambda i: None)
    assert w["done"] and w["reroutes"] == 0
    assert w["cues"] == ["arrive"]
    assert said(w, "You have arrived at the finish.")
    assert said(w, "Walk 10 meters to the finish.")
    assert not said(w, "doorway") and not said(w, "Entering")   # one room: nothing to announce on the way
    assert all(b in (None, 0) for b in w["bearing"])
    assert 12 <= len(w["pos"]) <= 15                        # 10 m in 0.7 m steps


def test_line_drifting_30_degrees_gets_steering_taps_but_stays_on_the_route():
    w = walk(demomaps.LINE, lambda i: CAL + 30 if 4 <= i < 9 else None)   # compass up = turned right
    drift = w["bearing"][4:9]
    assert all(b == 30 for b in drift)                      # + means "turn left" to get back
    assert all(0 < Engine.steer_period(b) <= Engine.STEER_SLOW_MS for b in drift)
    assert Engine.steer_period(w["bearing"][2]) == 0        # quiet while on course
    assert w["done"] and w["reroutes"] == 0 and not said(w, "off the route")


def test_line_walking_off_at_70_degrees_is_told_and_brought_back():
    w = walk(demomaps.LINE, lambda i: CAL + 70 if 4 <= i < 11 else None)
    assert said(w, "You are off the route")
    assert "off_route" in w["cues"]
    assert w["reroutes"] == 1
    assert max(abs(x - 3.5) for x, _ in w["pos"]) > 2.0     # they really were put off to the side
    assert w["done"] and w["cues"][-1] == "arrive"


def test_line_length_comes_from_the_caller():
    g = walkgraph.build(demomaps.line(20))
    a = next(n for n in g.nodes.values() if n.label == demomaps.START)
    nav = Navigator(g, demomaps.GOAL, "blind")
    ev = nav.start(a.x, a.y, a.floor)
    assert "20 meters, 0 turns" in ev[0]["text"]


# --- the corner -----------------------------------------------------------------------------------

def test_corner_is_announced_cued_and_walked():
    w = walk(demomaps.CORNER, lambda i: None)
    assert said(w, "Route ready: 12 meters, 1 turn")
    assert said(w, "Walk 6 meters, then turn right")
    assert said(w, "Turn right now")
    assert w["cues"] == ["right", "arrive"]
    assert w["done"] and w["reroutes"] == 0
    assert not said(w, "doorway")


def test_corner_missed_holds_them_at_the_turn_and_keeps_tapping_right():
    w = walk(demomaps.CORNER, lambda i: CAL if i < 20 else None)   # walks straight on, long past the turn
    held = w["pos"][12:20]
    assert len(set(held)) == 1                              # not walked through the wall
    assert held[0][1] <= 8.0                                # still inside the corridor (it ends at y = 8)
    assert all(b is not None and b <= -60 for b in w["bearing"][12:20])       # - is "turn right"
    assert all(Engine.steer_period(b) <= 2500 for b in w["bearing"][12:20])  # and it is insistent
    assert w["done"]                                        # then they turn, and finish


def test_corner_can_turn_left_instead():
    g = walkgraph.build(demomaps.corner(6, 6, "left"))
    a = next(n for n in g.nodes.values() if n.label == demomaps.START)
    nav = Navigator(g, demomaps.GOAL, "blind")
    ev = nav.start(a.x, a.y, a.floor)
    assert "12 meters, 1 turn" in ev[0]["text"]
    assert "turn left" in ev[1]["text"]
    tr = Tracker(nav, geo=None)
    tr.calibrate(0.0)
    cues, compass = [], 0.0
    for _ in range(40):
        o = tr.step(1, compass, 0.7)
        cues += [e["cue"] for e in o["events"] if e.get("cue")]
        if o["done"]:
            break
        if o["bearing_to_next_deg"] is not None:
            compass = (compass - o["bearing_to_next_deg"]) % 360
    assert cues == ["left", "arrive"]


def test_sizes_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NUDGE_DEMO_LINE_M", "15")
    monkeypatch.setenv("NUDGE_DEMO_CORNER_M", "5,8")
    monkeypatch.setenv("NUDGE_DEMO_TURN", "left")
    z = next(p for p in demomaps.model(demomaps.LINE)["panoLocations"] if p["label"] == demomaps.GOAL)
    assert z["position"]["y"] == pytest.approx(16.0)        # 15 m past the start at y = 1
    z = next(p for p in demomaps.model(demomaps.CORNER)["panoLocations"] if p["label"] == demomaps.GOAL)
    assert (z["position"]["x"], z["position"]["y"]) == pytest.approx((2.0 - 9.0, 6.0))   # mirrored: left of the start
    monkeypatch.setenv("NUDGE_DEMO_LINE_M", "nonsense")
    assert math.isclose(next(p for p in demomaps.model(demomaps.LINE)["panoLocations"]
                             if p["label"] == demomaps.GOAL)["position"]["y"], 11.0)
