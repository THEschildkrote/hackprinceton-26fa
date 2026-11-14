"""the collar attached to a server session.

these run the real ticker thread, so a couple of them sleep. the sleeps are generous: the
point is that the thread renders at all, not that it hits a deadline.
"""
import time

import pytest
from fastapi.testclient import TestClient

from nudge import bandserve, walkgraph
from nudge.band import REST_DEG
from nudge.router import plan
from nudge.server import app
from nudge.sim import walk
from tests.synthetic import building

c = TestClient(app)
GOAL = "Conference Room"


@pytest.fixture(autouse=True)
def released():
    """never leave the collar bound to a dead session, whatever the test did."""
    yield
    bandserve.detach()


def start_session(start="p_K3"):
    g = walkgraph.build(building())
    s = g.nodes[start]
    r = c.post("/api/nav/start", json={"sid": "synthetic", "to": GOAL, "profile": "blind",
                                       "x": s.x, "y": s.y, "floor": s.floor, "heading": 0.0})
    assert r.status_code == 200, r.text
    return g, r.json()["session"], plan(g, start, GOAL, "blind"), s


def strokes(band):
    n, out = 0, False
    for _, left, right in band.commands:
        now_out = any(v is not None and v > REST_DEG for v in (left, right))
        if now_out and not out:
            n += 1
        out = now_out
    return n


# --- attaching ----------------------------------------------------------------


def test_the_band_is_off_until_something_attaches_it():
    st = c.get("/api/band").json()
    assert st["attached"] is False and st["session"] is None


def test_attach_binds_to_a_session_and_detach_releases_it():
    _, session, _, _ = start_session()
    st = c.post("/api/band/attach", json={"session": session, "dry": True}).json()
    assert st["attached"] is True and st["session"] == session and st["mode"] == "dry"
    assert c.get("/api/band").json()["session"] == session

    st = c.post("/api/band/detach").json()
    assert st["attached"] is False and st["session"] is None


def test_attaching_moves_the_band_off_the_old_session():
    _, first, _, _ = start_session()
    _, second, _, _ = start_session()
    c.post("/api/band/attach", json={"session": first, "dry": True})
    c.post("/api/band/attach", json={"session": second, "dry": True})
    assert c.get("/api/band").json()["session"] == second


def test_attaching_to_an_unknown_session_is_a_404():
    assert c.post("/api/band/attach", json={"session": "nope", "dry": True}).status_code == 404


def test_the_ticker_runs_while_attached():
    _, session, _, _ = start_session()
    c.post("/api/band/attach", json={"session": session, "dry": True})
    assert bandserve.SERVICE._thread is not None and bandserve.SERVICE._thread.is_alive()
    c.post("/api/band/detach")
    time.sleep(0.05)
    assert bandserve.SERVICE._runner is None


# --- a walk through the api ----------------------------------------------------


def test_a_walk_over_http_taps_the_shoulders():
    g, session, route, s = start_session()
    c.post("/api/band/attach", json={"session": session, "dry": True})
    band = bandserve.SERVICE._runner.band

    for x, y, floor, h in list(walk(g, route, step=0.5))[:40]:
        c.post("/api/nav/update", json={"session": session, "x": x, "y": y,
                                        "floor": floor, "heading": h})
        time.sleep(0.02)  # let the ticker render between positions

    assert strokes(band) > 0, "walked the route over http without tapping anyone"


def test_detaching_releases_the_servos():
    g, session, route, s = start_session()
    c.post("/api/band/attach", json={"session": session, "dry": True})
    band = bandserve.SERVICE._runner.band
    for x, y, floor, h in list(walk(g, route, step=0.5))[:10]:
        c.post("/api/nav/update", json={"session": session, "x": x, "y": y,
                                        "floor": floor, "heading": h})
        time.sleep(0.02)
    c.post("/api/band/detach")
    assert band.commands[-1][1:] == (None, None), "left a servo engaged after detach"


# --- it must never break the walk ----------------------------------------------


def test_a_broken_band_does_not_break_navigation(monkeypatch):
    """the whole point of _band_sync swallowing: you lose the taps, not the route."""
    g, session, route, s = start_session()
    c.post("/api/band/attach", json={"session": session, "dry": True})

    def boom(*a, **kw):
        raise RuntimeError("servo on fire")

    monkeypatch.setattr(bandserve, "sync", boom)
    x, y, floor, h = next(iter(walk(g, route, step=0.5)))
    r = c.post("/api/nav/update", json={"session": session, "x": x, "y": y,
                                        "floor": floor, "heading": h})
    assert r.status_code == 200
    assert r.json()["state"]["route"]["length"] > 0


def test_sync_ignores_a_session_that_is_not_attached():
    g, first, _, _ = start_session()
    _, second, _, _ = start_session()
    c.post("/api/band/attach", json={"session": first, "dry": True})
    assert bandserve.sync(second, bandserve.SERVICE._runner.nav) is False


def test_starting_a_walk_with_no_band_attached_is_fine():
    _, session, _, _ = start_session()
    assert c.get("/api/band").json()["attached"] is False
    g = walkgraph.build(building())
    route = plan(g, "p_K3", GOAL, "blind")
    x, y, floor, h = next(iter(walk(g, route, step=0.5)))
    r = c.post("/api/nav/update", json={"session": session, "x": x, "y": y,
                                        "floor": floor, "heading": h})
    assert r.status_code == 200
