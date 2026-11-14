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
            return 200, {"state": self.state, "calibrated": self.calibrated, "sensor_ok": True, "paused": False,
                         "pose": self.pose, "target": 1, "distance_m": None}
        if path == "/route":
            if self.state == "NAVIGATING":
                return 409, {"ok": False, "error": "guidance is running: stop first, then load the route"}
            pts = body["route"]["points"]
            self.waypoints = [p[:2] for p in pts if p[2] == pts[0][2]]
            self.state, self.calibrated = "IDLE", False
            return 200, {"ok": True, "waypoints": len(self.waypoints), "given": len(pts),
                         "dropped_other_floors": len(pts) - len(self.waypoints)}
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
    assert [p for p, _ in fake.posts()] == ["/route", "/calibrate", "/start"]
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
