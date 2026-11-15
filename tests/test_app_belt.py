"""the whole demo path with the belt's own runtime, driven exactly the way the Isaac apps drive the server.

the caretaker sends a trip, the traveller's app starts it, counts down, calibrates on its compass and posts
steps; the server passes them on to the belt, the belt keeps the position and taps, and what the navigator
says comes back to the app in its step replies. voice turns go through /api/voice/turn as the app sends them.

the belt here is a simulator, not the real runtime: it dead-reckons from the app's step count and compass,
lined up at /calibrate on "the wearer is on the first point, facing the second", the way the real one is
described in docs/belt.md. it is good enough to check the contract end to end; it is not the belt.
"""
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from nudge import belt, fetch, server
from tests import belt_maps


@pytest.fixture(autouse=True)
def _belt_maps(monkeypatch):
    """the two drawn floor plans (tests/belt_maps.py) load like any building, for these tests only."""
    from nudge.agents import tools as T
    real = fetch.load
    monkeypatch.setattr(fetch, "load",
                        lambda sid, refresh=False: belt_maps.model(sid) if sid in belt_maps.NAMES else real(sid, refresh))
    T._graph_cached.cache_clear()
    yield
    T._graph_cached.cache_clear()

c = TestClient(server.app)
STEP_M = 0.7
CAL = 137.0        # the phone's compass, whatever it reads when the wearer faces the first leg


def bearing(a, b):
    """compass style in the route frame, as the belt speaks it: degrees clockwise from +y."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def wrap(d):
    return (d + 180) % 360 - 180


class SimBelt:
    """a belt runtime that tracks by dead reckoning from the app's readings and taps toward the route."""

    def __init__(self, step_m=STEP_M):
        self.step_m, self.calls = step_m, []
        self.reset()
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._answer(json.loads(self.rfile.read(n)) if n else None)

            def _answer(self, body):
                with me.lock:
                    code, out = me.handle(self.command, self.path, body)
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.state, self.calibrated, self.pts, self.target = "IDLE", False, [], 1
        self.pose, self.offset, self.count, self.heading, self.taps, self.floor = None, None, None, None, [], None

    def tap(self, kind, side=None, reason=None):
        self.taps.append({"seq": len(self.taps) + 1, "kind": kind, "side": side, "reason": reason})

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/state":
            return 200, {"state": self.state, "calibrated": self.calibrated, "sensor_ok": self.count is not None,
                         "paused": False, "pose": self.pose, "target": self.target, "taps": list(self.taps),
                         "sensor": {"step_count": self.count or 0, "heading_deg": self.heading},
                         "localizer": {"step_length_m": self.step_m},
                         "route": {"floor": self.floor, "floor_index": 0, "floors": 1}}
        if path == "/route":
            if body.get("reroute"):
                if not self.calibrated:
                    return 409, {"ok": False, "error": "not calibrated"}
            elif self.state == "NAVIGATING":
                return 409, {"ok": False, "error": "guidance is running: stop first, then load the route"}
            else:
                self.state, self.calibrated = "IDLE", False
            pts = body["route"]["points"]
            self.floor = pts[0][2]
            self.pts, self.target = [tuple(p[:2]) for p in pts if p[2] == self.floor], 1
            return 200, {"ok": True, "waypoints": len(self.pts), "given": len(pts), "dropped_other_floors": 0}
        if path == "/calibrate":
            if len(self.pts) < 2 or self.heading is None:
                return 409, {"ok": False, "error": "no route or no sensor"}
            first = bearing(self.pts[0], self.pts[1])
            self.offset = wrap(first - self.heading)
            self.pose = {"x": self.pts[0][0], "y": self.pts[0][1], "heading_deg": first}
            self.calibrated = True
            return 200, {"ok": True, "heading_deg": first}
        if path == "/start":
            if not self.calibrated:
                return 409, {"ok": False, "error": "not calibrated"}
            self.state = "NAVIGATING"
            return 200, {"ok": True}
        if path == "/stop":
            if self.state == "NAVIGATING":
                self.state = "STOPPED"
            return 200, {"ok": True}
        if path == "/sensor":
            n, h = int(body["step_count"]), float(body["heading_deg"])
            moved = 0 if self.count is None else max(0, n - self.count)
            self.count, self.heading = n, h
            if self.state == "NAVIGATING" and self.offset is not None:
                hb = (h + self.offset) % 360
                self.pose = {"x": self.pose["x"] + moved * self.step_m * math.sin(math.radians(hb)),
                             "y": self.pose["y"] + moved * self.step_m * math.cos(math.radians(hb)),
                             "heading_deg": hb}
                self._guide()
            return 200, {"ok": True}
        return 404, {"ok": False, "error": "not found"}

    def _guide(self):
        here = (self.pose["x"], self.pose["y"])
        while self.target < len(self.pts) - 1 and math.dist(here, self.pts[self.target]) < 0.8:
            self.target += 1
        if math.dist(here, self.pts[-1]) < 0.8:
            self.state = "ARRIVED"
            self.tap("arrived")
            return
        off = wrap(bearing(here, self.pts[self.target]) - self.pose["heading_deg"])
        if abs(off) > 20:
            self.tap("double_tap" if abs(off) > 60 else "tap", "right" if off > 0 else "left")

    def to_target_compass(self):
        """the phone compass a walker who obeys would face now: toward the belt's next waypoint."""
        here = (self.pose["x"], self.pose["y"])
        return (bearing(here, self.pts[self.target]) - self.offset) % 360

    def posts(self, path):
        return [b for m, p, b in self.calls if m == "POST" and p == path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def sim(monkeypatch):
    b = SimBelt()
    monkeypatch.setenv("NUDGE_BELT_URL", b.url)
    monkeypatch.delenv("NUDGE_BAND", raising=False)
    monkeypatch.setattr(belt, "POLL_S", 0.01)
    belt.detach()
    yield b
    belt.detach()
    b.close()


def settle(cond=None, timeout=2.0):
    """let the server's belt-following thread catch up with the belt."""
    end = time.time() + timeout
    while True:
        time.sleep(0.03)
        if cond is None or cond() or time.time() > end:
            return


class App:
    """the traveller's Isaac app, request for request (ios/Nudge/Traveller.swift)."""

    def __init__(self, tid, name="Sam"):
        self.tid, self.said, self.session, self.trip, self.done = tid, [], None, None, False
        r = c.post("/api/traveller/register", json={"traveller_id": tid, "name": name}).json()
        self.code = r["code"]

    def poll_trip(self):
        return c.get(f"/api/traveller/{self.tid}/trip", params={"heading_deg": CAL}).json().get("trip")

    def start(self, trip):
        r = c.post("/api/nav/start", json={"sid": trip["sid"], "to": trip["to"], "profile": trip["profile"],
                                           "start": "entrance", "traveller": self.tid})
        assert r.status_code == 200, r.text
        d = r.json()
        self.session, self.trip = d["session"], trip
        c.post(f"/api/traveller/{self.tid}/trip/ack")
        self.said += [e["text"] for e in d["events"] if e.get("text")]

    def calibrate(self):
        r = c.post("/api/nav/calibrate", json={"session": self.session, "heading_deg": CAL})
        assert r.status_code == 200, r.text

    def step(self, heading, steps=1):
        r = c.post("/api/nav/step", json={"session": self.session, "steps": steps, "heading_deg": heading,
                                          "step_m": STEP_M})
        assert r.status_code == 200, r.text
        o = r.json()
        self.said += o.get("say") or []
        self.done = self.done or bool(o.get("done"))
        return o

    def voice(self, text):
        body = {"traveller": self.tid, "text": text, "async_ok": True}
        if self.session:
            body["session"] = self.session
        if self.trip:
            body["sid"] = self.trip["sid"]
        r = c.post("/api/voice/turn", json=body)
        assert r.status_code == 200, r.text
        return r.json()


def walk(app, sim, max_steps=60, hold=None):
    """step until arrival. hold(i) -> a compass to keep, or None to obey the belt (face its next waypoint)."""
    for i in range(max_steps):
        h = hold(i) if hold else None
        app.step(sim.to_target_compass() if h is None else h)
        settle()
        if app.done:
            return i + 1
    for _ in range(5):          # the arrival may land between reports: the app's idle update picks it up
        app.step(sim.to_target_compass(), steps=0)
        settle()
        if app.done:
            return max_steps
    return None


def said(app, text):
    return any(text.lower() in s.lower() for s in app.said)


# --- the caretaker sends a trip, the app walks it ------------------------------------------------------

def test_a_caretaker_trip_on_the_corner_is_walked_by_the_belt_with_the_app_speaking(sim):
    app = App("e2e-corner")
    c.post(f"/api/traveller/{app.tid}/trip", json={"sid": belt_maps.CORNER, "to": "Finish", "profile": "blind"})
    trip = app.poll_trip()
    assert trip and trip["sid"] == belt_maps.CORNER
    app.start(trip)
    assert sim.posts("/route"), "the route went to the belt"
    app.calibrate()
    assert sim.state == "NAVIGATING" and sim.calibrated
    n = walk(app, sim)
    assert n is not None, f"never arrived; said {app.said}"
    assert said(app, "Route ready: 12 meters, 1 turn")
    assert said(app, "Turn right now"), app.said
    assert said(app, "You have arrived"), app.said
    assert len(sim.posts("/sensor")) >= n                        # every step report reached the belt
    st = c.get(f"/api/traveller/{app.tid}/status").json()
    assert st["state"] == "arrived", st


def test_a_straight_walk_that_drifts_gets_taps_from_the_belt_and_still_arrives(sim):
    app = App("e2e-line")
    c.post(f"/api/traveller/{app.tid}/trip", json={"sid": belt_maps.LINE, "to": "Finish", "profile": "blind"})
    app.start(app.poll_trip())
    app.calibrate()
    n = walk(app, sim, hold=lambda i: (CAL + 35) % 360 if 3 <= i < 6 else None)
    assert n is not None, app.said
    assert any(t["kind"] in ("tap", "double_tap") and t["side"] == "left" for t in sim.taps)
    assert said(app, "You have arrived")


# --- talking to it mid-walk ------------------------------------------------------------------------------

def test_voice_mid_walk_answers_from_the_belts_position(sim):
    app = App("e2e-voice")
    c.post(f"/api/traveller/{app.tid}/trip", json={"sid": belt_maps.LINE, "to": "Finish", "profile": "blind"})
    app.start(app.poll_trip())
    app.calibrate()
    for _ in range(6):
        app.step(sim.to_target_compass())
        settle()
    c.post("/api/caretaker/pair", json={"code": app.code})
    c.get(f"/api/traveller/{app.tid}/messages")       # the caretaker's feed is open before anything happens
    far = app.voice("how far")["say"]
    assert "to go" in far, far
    meters_left = float(far.split("About ")[1].split(" ")[0])
    assert 4.0 <= meters_left <= 7.0, far                       # 10 m walk, 6 steps of 0.7 m in
    assert app.voice("where am i")["say"]
    h = app.voice("I need help")
    assert "caretaker" in h["say"].lower()
    msgs = c.get(f"/api/traveller/{app.tid}/messages").json()["messages"]
    assert any(m.get("urgent") and "asked for help" in m["text"] for m in msgs), msgs


# --- a trip asked for by voice ---------------------------------------------------------------------------

def test_a_trip_asked_for_by_voice_reaches_the_belt_and_the_app_can_walk_it(sim):
    app = App("e2e-voicetrip")
    app.trip = {"sid": belt_maps.CORNER, "to": "Finish", "profile": "blind"}   # the building it's in
    r = app.voice("take me to the finish")
    assert r.get("session"), r
    assert "going to" in r["say"].lower(), r["say"]
    settle(lambda: belt.holds(r["session"]))     # the hand-off runs off the voice request's thread
    assert sim.posts("/route"), "the voice trip's route reached the belt"
    # the app adopts the session the voice reply named, counts down and calibrates, as for any trip
    app.session = r["session"]
    app.calibrate()
    assert sim.state == "NAVIGATING"
    assert walk(app, sim) is not None, app.said
    assert said(app, "Turn right now") and said(app, "You have arrived")


def test_a_voice_trip_starts_where_an_app_trip_would(sim):
    app = App("e2e-voicestart")
    app.trip = {"sid": belt_maps.CORNER, "to": "Finish", "profile": "blind"}
    v = app.voice("take me to the finish")
    vpts = v["state"]["path"]
    a = App("e2e-appstart")
    c.post(f"/api/traveller/{a.tid}/trip", json={"sid": belt_maps.CORNER, "to": "Finish", "profile": "blind"})
    a.start(a.poll_trip())
    apts = server._sessions[a.session].state()["path"]
    assert vpts[0][:2] == pytest.approx([1.0, 1.0]), vpts[0]       # the start mark, not some other point
    assert apts[0][:2] == pytest.approx(vpts[0][:2])


def test_a_trip_asked_for_mid_walk_starts_where_the_wearer_is_and_the_belt_carries_on(sim):
    # the app keeps walking and reports to the new trip (Traveller.adopt): no countdown, no calibrate.
    # so the new route has to begin where the belt has the wearer, or the belt stops and waits
    app = App("e2e-midwalk")
    c.post(f"/api/traveller/{app.tid}/trip", json={"sid": belt_maps.CORNER, "to": "Finish", "profile": "blind"})
    app.start(app.poll_trip())
    app.calibrate()
    for _ in range(4):
        app.step(sim.to_target_compass())
        settle()
    here = (sim.pose["x"], sim.pose["y"])
    r = app.voice("take me to the finish")
    assert r["session"] != app.session, r
    start = r["state"]["path"][0]
    assert math.dist(start[:2], here) < 2.0, (start, here)   # the nearest point on from here, not the start mark
    assert math.dist(start[:2], (1.0, 1.0)) > 2.0
    settle(lambda: belt.holds(r["session"]))
    rerouted = [b for b in sim.posts("/route") if b.get("reroute")]
    assert rerouted, "the new route went as a continuation, keeping the belt's calibration"
    assert sim.state == "NAVIGATING"
    app.session = r["session"]
    assert walk(app, sim) is not None, app.said
    assert said(app, "You have arrived")
