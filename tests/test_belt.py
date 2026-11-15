"""the belt hand-off: our route goes to the pi's on-body runtime, its position comes back.

the belt here is a stand-in that answers the five calls the real runtime does, with the same refusals.
"""
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from nudge import belt, server

c = TestClient(server.app)
START = {"sid": "synthetic", "to": "Conference Room", "profile": "blind", "x": 2, "y": 1.5, "floor": "f1"}


class FakeBelt:
    def __init__(self):
        self.calls, self.state, self.pose, self.waypoints, self.calibrated = [], "IDLE", None, [], False
        self.steps, self.taps, self.floors, self.floor_index, self.old = 0, [], [], 0, False
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._answer(json.loads(self.rfile.read(n)) if n else None)

            def _answer(self, body):
                code, out = me.handle(self.command, self.path, body)
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/state":
            out = {"state": self.state, "calibrated": self.calibrated, "sensor_ok": True, "paused": False,
                   "pose": self.pose, "target": 1, "distance_m": None}
            if not self.old:  # a belt runtime that knows floors, and shows its phone's steps and its own taps
                out.update(sensor={"step_count": self.steps, "heading_deg": 0.0}, taps=list(self.taps),
                           localizer={"step_length_m": 0.5},
                           route={"floor": self.floors[self.floor_index] if self.floors else None,
                                  "floor_index": self.floor_index, "floors": len(self.floors)})
            return 200, out
        if path == "/route":
            pts = body["route"]["points"]
            if body.get("reroute") and not self.old:
                if not self.calibrated:
                    return 409, {"ok": False, "error": "not calibrated: there is no position to carry on from"}
            elif self.state == "NAVIGATING":
                return 409, {"ok": False, "error": "guidance is running: stop first, then load the route"}
            else:
                self.state, self.calibrated = "IDLE", False
            self.waypoints = [p[:2] for p in pts if p[2] == pts[0][2]]
            self.floors, self.floor_index = list(dict.fromkeys(p[2] for p in pts)), 0
            return 200, {"ok": True, "waypoints": len(self.waypoints), "given": len(pts),
                         "dropped_other_floors": len(pts) - len(self.waypoints), "floors": len(self.floors)}
        if path == "/mark":
            if not self.calibrated:
                return 409, {"ok": False, "error": "not calibrated: a mark corrects a position, and there is none yet"}
            return 200, {"ok": True, "waypoint": (body or {}).get("waypoint", 1), "moved_m": 1.5}
        if path == "/sensor":
            self.steps, self.heading = body["step_count"], body["heading_deg"]
            return 200, {"ok": True}
        if path == "/floor":
            if self.floor_index + 1 >= len(self.floors):
                return 409, {"ok": False, "error": "this route has no further floor"}
            self.floor_index += 1
            self.state = "NAVIGATING"
            return 200, {"ok": True, "floor": self.floors[self.floor_index], "floor_index": self.floor_index}
        if path == "/calibrate":
            self.calibrated = True
            return 200, {"ok": True}
        if path == "/start":
            if not self.calibrated:
                return 409, {"ok": False, "error": "not calibrated"}
            self.state = "NAVIGATING"
            return 200, {"ok": True}
        if path == "/stop":
            self.state = "STOPPED" if self.state == "NAVIGATING" else self.state
            return 200, {"ok": True}
        if path == "/wearer":
            return 200, {"ok": True, "height_cm": body["height_cm"], "step_length_m": 0.5 * body["height_cm"] / 175}
        return 404, {"ok": False, "error": "not found"}

    def posts(self):
        return [(p, b) for m, p, b in self.calls if m == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake(monkeypatch):
    f = FakeBelt()
    monkeypatch.setenv("NUDGE_BELT_URL", f.url)
    monkeypatch.setattr(belt, "POLL_S", 0.02)
    belt.detach()
    yield f
    belt.detach()
    f.close()


def wait(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.01)


def test_nothing_is_sent_without_a_belt_url(monkeypatch):
    monkeypatch.delenv("NUDGE_BELT_URL", raising=False)
    belt.detach()
    s = c.post("/api/nav/start", json=START)
    assert s.status_code == 200
    st = c.get("/api/belt").json()
    assert st["enabled"] is False and st["session"] is None and st["belt"] is None


def test_a_walk_start_sends_the_route_as_api_route_returns_it(fake):
    s = c.post("/api/nav/start", json=START).json()
    path, body = fake.posts()[0]
    assert path == "/route"
    assert body == {"route": {"points": s["state"]["route"]["points"]}}
    assert all(len(p) == 3 for p in body["route"]["points"])          # x, y, floor: the belt reads all three
    st = c.get("/api/belt").json()
    assert st["enabled"] and st["session"] == s["session"] and st["error"] is None
    assert st["route"]["dropped_other_floors"] == len(body["route"]["points"]) - len(fake.waypoints)
    assert fake.state == "IDLE" and not st["walking"]                 # nothing moves until the wearer is lined up


def test_calibrate_lines_the_belt_up_and_starts_it(fake):
    s = c.post("/api/nav/start", json=START).json()
    r = c.post("/api/nav/calibrate", json={"session": s["session"], "heading_deg": 90.0})
    assert r.status_code == 200
    # the phone's compass reading goes to the belt first (it lines up on a reading), then it is lined up and started
    order = [p for p, _ in fake.posts()]
    assert order.index("/sensor") < order.index("/calibrate")
    assert [p for p in order if p != "/sensor"] == ["/route", "/calibrate", "/start"]
    assert fake.state == "NAVIGATING" and c.get("/api/belt").json()["walking"]


def test_the_belts_position_moves_our_navigator(fake):
    s = c.post("/api/nav/start", json=START).json()
    nav = server.get_navigator(s["session"])
    c.post("/api/belt/begin", json={"session": s["session"]})
    # the belt reports where the wearer is in our own frame, heading as a compass in that frame:
    # 90 degrees clockwise from +y is along +x, which is 0 radians for us.
    fake.pose = {"x": 4.0, "y": 1.5, "heading_deg": 90.0}
    wait(lambda: nav.pos is not None and abs(nav.pos[0] - 4.0) < 1e-6)
    assert nav.pos[:3] == (4.0, 1.5, "f1")
    assert abs(nav.heading) < 1e-6
    assert nav.progress > 1.5                                           # two metres along the first leg


def test_arrival_at_the_belt_ends_the_feed(fake):
    s = c.post("/api/nav/start", json=START).json()
    c.post("/api/belt/begin", json={"session": s["session"]})
    fake.pose, fake.state = {"x": 13.0, "y": 7.0, "heading_deg": 0.0}, "ARRIVED"
    wait(lambda: not c.get("/api/belt").json()["walking"])
    polls = len(fake.calls)
    time.sleep(0.15)
    assert len([x for x in fake.calls[polls:] if x[1] == "/state"]) <= 1   # only the status call above


def test_a_new_walk_takes_the_belt_over_and_stops_the_old_one(fake):
    first = c.post("/api/nav/start", json=START).json()
    c.post("/api/belt/begin", json={"session": first["session"]})
    assert fake.state == "NAVIGATING"
    second = c.post("/api/nav/start", json=dict(START, to="Kitchen")).json()
    assert [p for p, _ in fake.posts()][-2:] == ["/stop", "/route"]    # it refuses a route while guiding
    assert c.get("/api/belt").json()["session"] == second["session"]


def test_a_belt_that_is_off_never_breaks_a_walk(monkeypatch):
    monkeypatch.setenv("NUDGE_BELT_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(belt, "TIMEOUT_S", 0.5)
    belt.detach()
    s = c.post("/api/nav/start", json=START)
    assert s.status_code == 200 and s.json()["state"]["route"]["length"] > 0
    assert c.post("/api/nav/calibrate", json={"session": s.json()["session"], "heading_deg": 0.0}).status_code == 200
    st = c.get("/api/belt").json()
    assert st["enabled"] and st["error"] and st["belt"] is None
    belt.detach()


def test_a_refusal_is_kept_for_the_status_page(fake):
    s = c.post("/api/nav/start", json=START).json()
    fake.calibrated = False
    assert belt._try(belt._call, "POST", "/start") is None
    assert "not calibrated" in c.get("/api/belt").json()["error"]
    assert c.post("/api/belt/stop").json()["ok"]
    assert c.post("/api/belt/wearer", json={"height_cm": 160}).json()["ok"]
    assert fake.posts()[-1] == ("/wearer", {"height_cm": 160.0})
    assert s["session"]


@pytest.mark.parametrize("belt_deg, ours", [(0, math.pi / 2), (90, 0.0), (180, -math.pi / 2), (270, math.pi), (450, 0.0)])
def test_heading_conversion(belt_deg, ours):
    got = belt.model_heading(belt_deg)
    assert abs(math.atan2(math.sin(got - ours), math.cos(got - ours))) < 1e-9


# -- the belt on the dashboard, reroutes, floors -----------------------------------------------------------


def traces_of(session, kind):
    from nudge import traces
    return [e for e in traces.recent(n=traces.RING) if e.get("kind") == kind and e.get("session") == session]


def test_the_belts_steps_and_taps_reach_the_dashboard(fake):
    fake.steps, fake.taps = 40, [{"seq": 7, "kind": "tap", "side": "left"}]     # what the belt did before this walk
    s = c.post("/api/nav/start", json=START).json()
    sid = s["session"]
    c.post("/api/belt/begin", json={"session": sid})
    wait(lambda: belt._state["tap_seq"] == 7)                                   # first look: counts taken, nothing told
    assert not traces_of(sid, "step") and not traces_of(sid, "band")
    fake.pose = {"x": 3.0, "y": 1.5, "heading_deg": 90.0}
    fake.steps = 43
    fake.taps += [{"seq": 8, "kind": "double_tap", "side": "right"},
                  {"seq": 9, "kind": "tap", "side": "left", "reason": "overturn"},
                  {"seq": 10, "kind": "stop", "side": None, "reason": "floor"}]
    wait(lambda: len(traces_of(sid, "band")) == 3)
    assert [e["cue"] for e in traces_of(sid, "band")] == ["right", "overturn_left", "floor"]
    assert all(e["mode"] == "belt" for e in traces_of(sid, "band"))
    steps = traces_of(sid, "step")
    assert sum(e["steps"] for e in steps) == 3 and steps[-1]["heading_deg"] == 90.0 and steps[-1]["step_m"] == 0.5
    snap = c.get("/api/ops/snapshot?events=0").json()
    assert snap["band"]["attached"] and snap["band"]["mode"] == "belt"          # haptics: the belt
    assert any(d["role"] == "phone" and d["online"] for d in snap["devices"])   # its phone counts as connected
    assert [e["heading_deg"] for e in traces_of(sid, "calibrate")] == []        # the stand-in gives no heading


@pytest.mark.parametrize("tap, word", [
    ({"kind": "tap", "side": "left"}, "steer_left"), ({"kind": "double_tap", "side": "right"}, "right"),
    ({"kind": "tap", "side": "right", "reason": "overturn"}, "overturn_right"),
    ({"kind": "stop", "side": None}, "stop"), ({"kind": "stop", "side": None, "reason": "floor"}, "floor"),
    ({"kind": "arrived", "side": None}, "arrive")])
def test_belt_taps_in_the_collars_cue_words(tap, word):
    assert belt.cue(tap) == word


def test_a_reroute_goes_to_the_belt_without_stopping_it(fake):
    s = c.post("/api/nav/start", json=START).json()
    sid = s["session"]
    nav = server.get_navigator(sid)
    c.post("/api/belt/begin", json={"session": sid})
    fake.pose = {"x": 2.0, "y": 1.5, "heading_deg": 90.0}
    wait(lambda: nav.pos is not None)
    before = list(s["state"]["route"]["points"])
    a, b = nav.route.nodes[1], nav.route.nodes[2]
    r = c.post("/api/nav/block", json={"session": sid, "x": 2.0, "y": 1.5, "floor": "f1", "a": a, "b": b})
    assert r.status_code == 200
    after = nav.route.to_dict(nav.g)["points"]
    assert after != before                                                       # the navigator found another way
    wait(lambda: fake.posts()[-1][0] == "/route" and fake.posts()[-1][1].get("reroute"))
    assert fake.posts()[-1] == ("/route", {"route": {"points": after}, "reroute": True})
    assert "/stop" not in [p for p, _ in fake.posts()] and fake.state == "NAVIGATING"   # nobody was stopped
    time.sleep(0.1)
    assert len([1 for p, _ in fake.posts() if p == "/route"]) == 2               # and it was sent once
    assert c.get("/api/belt").json()["error"] is None


def test_a_belt_too_old_for_reroutes_refuses_once_and_the_reason_is_kept(fake):
    fake.old = True
    s = c.post("/api/nav/start", json=START).json()
    sid = s["session"]
    nav = server.get_navigator(sid)
    c.post("/api/belt/begin", json={"session": sid})
    fake.pose = {"x": 2.0, "y": 1.5, "heading_deg": 90.0}
    wait(lambda: nav.pos is not None)
    c.post("/api/nav/block", json={"session": sid, "x": 2.0, "y": 1.5, "floor": "f1",
                                   "a": nav.route.nodes[1], "b": nav.route.nodes[2]})
    wait(lambda: len([1 for p, _ in fake.posts() if p == "/route"]) == 2)
    time.sleep(0.1)
    assert len([1 for p, _ in fake.posts() if p == "/route"]) == 2
    assert "stop first" in c.get("/api/belt").json()["error"]
    assert nav.pos[:3] == (2.0, 1.5, "f1")                                       # an old belt has no floor to report


def test_next_floor_is_passed_on_and_the_floor_follows_the_belt(fake):
    s = c.post("/api/nav/start", json=START).json()
    sid = s["session"]
    nav = server.get_navigator(sid)
    points = s["state"]["route"]["points"]
    floors = list(dict.fromkeys(p[2] for p in points))
    assert len(floors) == 2                                                      # this walk goes upstairs
    upstairs = next(p for p in points if p[2] == floors[1])
    c.post("/api/belt/begin", json={"session": sid})
    fake.pose, fake.state = {"x": points[0][0], "y": points[0][1], "heading_deg": 0.0}, "FLOOR"
    time.sleep(0.1)
    assert nav.pos is None or nav.pos[2] == floors[0]                            # waiting at the stairs: nothing applied
    fake.pose = {"x": upstairs[0], "y": upstairs[1], "heading_deg": 0.0}
    r = c.post("/api/belt/floor").json()
    assert r["ok"] and fake.posts()[-1][0] == "/floor" and fake.state == "NAVIGATING"
    wait(lambda: nav.pos is not None and nav.pos[2] == floors[1])                # our navigator is upstairs too
    assert c.get("/api/belt").json()["belt"]["floor"] == floors[1]
    assert not c.post("/api/belt/floor").json()["ok"]                            # there is no third floor
    assert "no further floor" in c.get("/api/belt").json()["error"]


def test_a_walk_asked_for_by_voice_goes_to_the_belt_too(fake):
    r = c.post("/api/voice/turn", json={"traveller": "belt-voice", "sid": "synthetic", "agent": False,
                                        "text": "take me to the conference room"})
    assert r.status_code == 200 and r.json()["session"], r.text
    sid = r.json()["session"]
    wait(lambda: any(p == "/route" for p, _ in fake.posts()))                    # sent off the request's thread
    path, body = next((p, b) for p, b in fake.posts() if p == "/route")
    nav = server.get_navigator(sid)
    assert body == {"route": {"points": nav.route.to_dict(nav.g)["points"]}}
    wait(lambda: c.get("/api/belt").json()["session"] == sid)
    assert fake.state == "IDLE"                                                  # nothing moves until they are lined up
    assert c.post("/api/belt/begin", json={"session": sid}).json()["ok"] and fake.state == "NAVIGATING"


# -- one walk after another, and loops ---------------------------------------------------------------------


def start_and_arrive(fake, to):
    s = c.post("/api/nav/start", json=dict(START, to=to)).json()
    nav = server.get_navigator(s["session"])
    c.post("/api/belt/begin", json={"session": s["session"]})
    end = nav.route.to_dict(nav.g)["points"][-1]
    fake.pose, fake.state = {"x": end[0], "y": end[1], "heading_deg": 0.0}, "ARRIVED"   # the belt walked them there
    wait(lambda: not c.get("/api/belt").json()["walking"])
    return s, end


def test_the_next_walk_carries_on_from_where_the_last_one_ended(fake):
    first, end = start_and_arrive(fake, "p_K2")
    before = len(fake.posts())
    second = c.post("/api/nav/start", json=dict(START, to="p_L1", x=end[0], y=end[1], floor=end[2])).json()
    sent = fake.posts()[before:]
    assert [p for p, _ in sent] == ["/route"] and sent[0][1]["reroute"] is True      # no stop, no fresh route
    assert sent[0][1]["route"]["points"] == second["state"]["route"]["points"]
    st = c.get("/api/belt").json()
    assert st["continued"] and st["session"] == second["session"] and st["error"] is None
    assert c.post("/api/belt/begin", json={"session": second["session"]}).json()["ok"]
    assert [p for p, _ in fake.posts()[before:]] == ["/route", "/start"]             # started, not lined up again
    assert fake.state == "NAVIGATING"


def test_a_walk_that_starts_somewhere_else_is_lined_up_afresh(fake):
    first, end = start_and_arrive(fake, "p_K2")
    before = len(fake.posts())
    second = c.post("/api/nav/start", json=dict(START, to="p_O3")).json()            # back at the lobby start
    assert [p for p, _ in fake.posts()[before:]] == ["/route"] and "reroute" not in fake.posts()[-1][1]
    assert not c.get("/api/belt").json()["continued"]
    c.post("/api/belt/begin", json={"session": second["session"]})
    assert [p for p, _ in fake.posts()[before:]] == ["/route", "/calibrate", "/start"]


def test_carrying_on_can_be_switched_off(fake, monkeypatch):
    monkeypatch.setenv("NUDGE_BELT_CONTINUE", "0")
    first, end = start_and_arrive(fake, "p_K2")
    before = len(fake.posts())
    c.post("/api/nav/start", json=dict(START, to="p_L1", x=end[0], y=end[1], floor=end[2]))
    assert "reroute" not in fake.posts()[-1][1] and not c.get("/api/belt").json()["continued"]
    assert [p for p, _ in fake.posts()[before:]] == ["/route"]


def test_an_old_belt_that_cannot_carry_on_is_lined_up_afresh(fake):
    first, end = start_and_arrive(fake, "p_K2")
    fake.old, fake.state = True, "NAVIGATING"            # an old runtime refuses a route while guiding
    before = len(fake.posts())
    c.post("/api/nav/start", json=dict(START, to="p_L1", x=end[0], y=end[1], floor=end[2]))
    assert [p for p, _ in fake.posts()[before:]] == ["/route", "/stop", "/route"]
    assert not c.get("/api/belt").json()["continued"] and c.get("/api/belt").json()["error"] is None


def test_the_way_back_can_avoid_the_way_out_which_makes_a_loop():
    out = c.post("/api/route", json={"sid": "synthetic", "from": "p_L1", "to": "p_L6"}).json()["route"]
    edges = [list(e) for e in zip(out["nodes"], out["nodes"][1:])]
    same = c.post("/api/route", json={"sid": "synthetic", "from": "p_L6", "to": "p_L1"}).json()["route"]
    assert same["nodes"] == out["nodes"][::-1]                                       # the shortest way back retraces
    back = c.post("/api/route", json={"sid": "synthetic", "from": "p_L6", "to": "p_L1", "avoid": edges}).json()["route"]
    walked = {frozenset(e) for e in edges}
    assert back["nodes"][0] == "p_L6" and back["nodes"][-1] == "p_L1"
    assert not walked & {frozenset(e) for e in zip(back["nodes"], back["nodes"][1:])}    # another way round
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": "p_L1", "profile": "blind", "x": 6.5, "y": 5.2,
                                       "floor": "f1", "avoid": edges}).json()
    nav = server.get_navigator(s["session"])
    assert not walked & {frozenset(e) for e in zip(nav.route.nodes, nav.route.nodes[1:])}


def test_a_belt_that_was_stopped_is_lined_up_afresh_even_if_it_thinks_it_is_near(fake):
    first, end = start_and_arrive(fake, "p_K2")
    fake.state = "STOPPED"                                # stopped, then perhaps carried somewhere: not believed
    before = len(fake.posts())
    c.post("/api/nav/start", json=dict(START, to="p_L1", x=end[0], y=end[1], floor=end[2]))
    assert [p for p, _ in fake.posts()[before:]] == ["/route"] and "reroute" not in fake.posts()[-1][1]
    assert not c.get("/api/belt").json()["continued"]


# -- the traveller's app as the belt's sensor --------------------------------------------------------------


def sensor_posts(fake):
    return [b for p, b in fake.posts() if p == "/sensor"]


def test_the_apps_compass_and_steps_go_to_the_belt_and_our_tracker_stays_out(fake):
    s = c.post("/api/nav/start", json=dict(START, to="p_K2")).json()
    sid = s["session"]
    nav = server.get_navigator(sid)
    assert c.post("/api/nav/calibrate", json={"session": sid, "heading_deg": 212.0}).status_code == 200
    order = [p for p, _ in fake.posts()]
    assert order.index("/sensor") < order.index("/calibrate") < order.index("/start")    # a reading first, then line up on it
    assert sensor_posts(fake)[0] == {"heading_deg": 212.0, "step_count": 0, "sid": "isaac-app"}
    r = c.post("/api/nav/step", json={"session": sid, "steps": 2, "heading_deg": 214.0, "step_m": 0.7}).json()
    assert {"heading_deg": 214.0, "step_count": 2, "sid": "isaac-app"} in sensor_posts(fake)
    c.post("/api/nav/step", json={"session": sid, "steps": 1, "heading_deg": 215.0})
    assert fake.steps == 3 and fake.heading == 215.0                              # a running count, the latest compass
    assert nav.progress == 0 and r["done"] is False and r["say"] == []            # 1.4 m of steps moved nothing here
    fake.pose = {"x": 3.5, "y": 3.0, "heading_deg": 45.0}                         # the belt is what moves the navigator
    wait(lambda: nav.pos is not None and abs(nav.pos[0] - 3.5) < 1e-6)
    r = c.post("/api/nav/step", json={"session": sid, "steps": 1, "heading_deg": 215.0}).json()
    assert r["position"] == {"x": 3.5, "y": 3.0, "floor": "f1"} and r["state"]["route"]
    assert sum(e["steps"] for e in traces_of(sid, "step")) == 4                   # the app's steps, counted once


def test_what_the_navigator_says_on_a_belt_walk_reaches_the_app(fake):
    s = c.post("/api/nav/start", json=dict(START, to="p_K2")).json()
    sid = s["session"]
    nav = server.get_navigator(sid)
    c.post("/api/nav/calibrate", json={"session": sid, "heading_deg": 0.0})
    pts = nav.route.to_dict(nav.g)["points"]
    for p in pts[1:]:                                                              # the belt walks the route
        fake.pose = {"x": p[0], "y": p[1], "heading_deg": 0.0}
        wait(lambda p=p: nav.pos is not None and abs(nav.pos[0] - p[0]) < 1e-6 and abs(nav.pos[1] - p[1]) < 1e-6)
    time.sleep(0.2)                                                                # a room is announced a moment after entering it
    r = c.post("/api/nav/step", json={"session": sid, "steps": 1, "heading_deg": 0.0}).json()
    assert r["say"] and r["done"] is True                                          # the turns, and the arrival
    time.sleep(0.2)
    again = c.post("/api/nav/step", json={"session": sid, "steps": 0, "heading_deg": 0.0}).json()
    assert again["say"] == []                                                      # each line is passed on once


def test_the_last_reading_is_repeated_between_reports_and_then_let_go(fake, monkeypatch):
    s = c.post("/api/nav/start", json=dict(START, to="p_K2")).json()
    sid = s["session"]
    c.post("/api/nav/calibrate", json={"session": sid, "heading_deg": 10.0})
    c.post("/api/nav/step", json={"session": sid, "steps": 1, "heading_deg": 12.0})
    n = len(sensor_posts(fake))
    wait(lambda: len(sensor_posts(fake)) >= n + 3)                                 # kept alive by the follow thread
    assert all(b == {"heading_deg": 12.0, "step_count": 1, "sid": "isaac-app"} for b in sensor_posts(fake)[n:])
    monkeypatch.setattr(belt, "SENSOR_HOLD_S", 0.0)                                # the app has gone quiet for too long
    time.sleep(0.1)
    n = len(sensor_posts(fake))
    time.sleep(0.15)
    assert len(sensor_posts(fake)) == n                                            # so the belt will stop the wearer


def test_a_walk_the_belt_is_not_on_still_uses_our_own_tracker(fake):
    first = c.post("/api/nav/start", json=dict(START, to="p_K2")).json()
    second = c.post("/api/nav/start", json=dict(START, to="p_O3")).json()          # the belt goes to this one
    c.post("/api/nav/calibrate", json={"session": first["session"], "heading_deg": 0.0})
    before = len(sensor_posts(fake))
    r = c.post("/api/nav/step", json={"session": first["session"], "steps": 3, "heading_deg": 0.0}).json()
    assert len(sensor_posts(fake)) == before and "waiting_for" in r
    assert server.get_navigator(first["session"]).progress > 0                    # moved by our tracker, as before


APP_START = {"sid": "synthetic", "profile": "blind", "start": "entrance"}     # what the app sends: no coordinates


def test_the_apps_next_trip_starts_where_the_belt_has_the_wearer(fake):
    first, end = start_and_arrive(fake, "p_K2")
    before = len(fake.posts())
    second = c.post("/api/nav/start", json=dict(APP_START, to="p_L1")).json()
    assert (second["start"]["x"], second["start"]["y"]) == (end[0], end[1])      # not the entrance: where they stand
    pts = second["state"]["route"]["points"]
    assert math.hypot(pts[0][0] - end[0], pts[0][1] - end[1]) < 3.0
    assert c.get("/api/belt").json()["continued"]                                # and the belt carries on
    c.post("/api/nav/calibrate", json={"session": second["session"], "heading_deg": 30.0})
    assert [p for p, _ in fake.posts()[before:] if p != "/sensor"] == ["/route", "/start"]     # no lining up again


def test_nudge_start_says_where_the_apps_first_trip_begins(fake, monkeypatch):
    usual = c.post("/api/nav/start", json=dict(APP_START, to="p_K2")).json()["start"]
    belt.detach()
    monkeypatch.setenv("NUDGE_START", "p_O3")
    s = c.post("/api/nav/start", json=dict(APP_START, to="p_K2")).json()
    assert (s["start"]["x"], s["start"]["y"]) == (14.5, 1.5) != (usual["x"], usual["y"])
    named = c.post("/api/nav/start", json=dict(APP_START, to="p_K2", start="p_L3")).json()     # a start that is named wins
    assert (named["start"]["x"], named["start"]["y"]) == (2, 4.5)


def test_the_caretaker_can_say_the_wearer_is_at_the_corner(fake):
    s = c.post("/api/nav/start", json=dict(START, to="p_K2")).json()
    early = c.post("/api/belt/mark").json()
    assert not early["ok"] and "not calibrated" in early["error"]          # nothing to correct before the walk
    c.post("/api/belt/begin", json={"session": s["session"]})
    r = c.post("/api/belt/mark").json()
    assert r["ok"] and r["moved_m"] == 1.5 and fake.posts()[-1] == ("/mark", {})
    assert c.post("/api/belt/mark", json={"waypoint": 2}).json()["ok"] and fake.posts()[-1] == ("/mark", {"waypoint": 2})


def test_a_spoken_request_that_names_no_building_means_the_one_the_server_is_set_up_for(monkeypatch):
    from nudge import buildings
    assert server._voice_building() == buildings.VENUE_SID
    monkeypatch.setenv("NUDGE_SID", "synthetic")
    assert server._voice_building() == "synthetic"
