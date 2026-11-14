"""live walking from phone steps + compass, kept on the walk graph (nudge/pdr.py, /api/nav/step)."""
import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nudge import walkgraph
from nudge.band import NullBand
from nudge.bandrun import BandRunner
from nudge.navigator import Navigator
from nudge.pdr import Tracker, make_walk
from tests.synthetic import building

ROOT = Path(__file__).resolve().parent.parent
REAL = "MgGNVWsyXtH"


def compass_for(tr, model_heading):
    """the compass a phone would read while facing `model_heading`, given the tracker's calibration."""
    base = model_heading - tr.corr          # model = -radians(c) + corr, with no georef
    return (-math.degrees(base)) % 360


def walk_route(tr, now=0.0, limit=200):
    """step along whatever route the tracker is on, facing the way it goes. returns all events."""
    events = []
    for i in range(limit):
        r = tr.route_heading()
        c = compass_for(tr, r) if r is not None else None
        out = tr.step(1, c, now=now + i * 0.5)
        events += out["events"]
        if out["done"]:
            break
    return events


@pytest.fixture
def syn():
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    k3 = g.nodes["p_K3"]
    nav.start(k3.x, k3.y, k3.floor)
    tr = Tracker(nav)
    tr.calibrate(0.0)
    return nav, tr


def test_following_the_route_arrives_with_turns(syn):
    nav, tr = syn
    events = walk_route(tr)
    kinds = [e["type"] for e in events]
    assert nav.done and "arrive" in kinds
    assert any(e["type"] == "now" and "left" in e["text"].lower() for e in events), [e["text"] for e in events]
    assert nav.reroutes == 0


def test_heading_only_updates_move_nothing(syn):
    nav, tr = syn
    before = tr.pos
    out = tr.step(0, 123.0, now=1.0)
    assert tr.pos == before and out["position"]["x"] == before[0]


def test_brief_heading_noise_is_ignored(syn):
    nav, tr = syn
    r = tr.route_heading()
    tr.step(1, compass_for(tr, r + math.radians(80)), now=0.5)   # one wild reading: held, not believed
    assert tr.mode == "route" and tr.along == 0
    tr.step(1, compass_for(tr, r), now=1.0)
    assert tr.mode == "route" and tr.along > 0 and tr.off_steps == 0


def test_wrong_turn_reroutes():
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    l1 = g.nodes["p_L1"]
    nav.start(l1.x, l1.y, l1.floor)
    tr = Tracker(nav)
    tr.calibrate(0.0)
    r = tr.route_heading()
    # keep walking 90 deg left of the route (north, toward the lobby's far side) for a while
    for i in range(8):
        tr.step(1, compass_for(tr, r + math.radians(90)), now=i * 0.5)
    assert tr.mode in ("free", "route") and nav.reroutes >= 1


def test_turning_around_retraces(syn):
    nav, tr = syn
    for i in range(4):
        tr.step(1, compass_for(tr, tr.route_heading()), now=i * 0.5)
    ahead = tr.along
    back = tr.route_heading() + math.pi
    for i in range(4):
        tr.step(1, compass_for(tr, back), now=3 + i * 0.5)
    assert tr.along < ahead and tr.mode == "route"


def test_stairs_take_a_flight_of_steps():
    g = walkgraph.build(building())
    nav = Navigator(g, "Conference Room", "blind")   # blind may take the stairs; force them
    nav.blocked_nodes.add("p_E1")
    o3 = g.nodes["p_O3"]
    nav.start(o3.x, o3.y, o3.floor)
    tr = Tracker(nav)
    tr.calibrate(0.0)
    events = walk_route(tr)
    assert nav.done, [e["text"] for e in events]
    assert any("stairs" in e["text"].lower() for e in events)


def test_band_gets_cues_during_a_replay(tmp_path, syn):
    nav, tr = syn
    band = NullBand()
    runner = BandRunner(nav, band=band)
    walk = tmp_path / "walk.jsonl"
    make_walk(tr, walk)
    t = 0
    for line in walk.read_text().splitlines():
        rec = json.loads(line)
        out = tr.step(rec["steps"], rec["heading_deg"], now=rec["t"])
        t = int(rec["t"] * 1000)
        runner.sync(out["heading_model"], t)
        for k in range(100):   # half a second of ticks between steps
            runner.tick(t + 5 * k)
        if out["done"]:
            break
    assert nav.done and len(band.commands) > 2


def test_server_start_calibrate_step(monkeypatch):
    from nudge import bandserve
    from nudge import server
    monkeypatch.setenv("NUDGE_BAND", "dry")
    bandserve.detach()
    c = TestClient(server.app)
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Office", "profile": "blind"}).json()
    sid = s["session"]
    assert s["start"]["floor"] == "f1" and s["state"]["route"]
    assert bandserve.status()["attached"] and bandserve.status()["mode"] == "dry"
    cal = c.post("/api/nav/calibrate", json={"session": sid, "heading_deg": 0}).json()
    assert cal["ok"]
    tr = server._trackers[sid]
    done = False
    for _ in range(200):
        r = tr.route_heading()
        out = c.post("/api/nav/step", json={"session": sid, "steps": 1,
                                            "heading_deg": compass_for(tr, r) if r is not None else None}).json()
        assert set(out) >= {"say", "events", "state", "device", "position", "distance_to_next_m",
                            "bearing_to_next_deg", "done"}
        if out["done"]:
            done = True
            break
    assert done and any("arrived" in s.lower() for s in out["say"])
    still = c.post("/api/nav/step", json={"session": sid, "steps": 0, "heading_deg": 90}).json()
    assert still["position"] == out["position"]
    bandserve.detach()


def test_server_start_by_place_name():
    from nudge import server
    c = TestClient(server.app)
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Office", "start": "Kitchen"}).json()
    assert s["state"]["route"] and s["start"]["floor"] == "f1"
    bad = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Office", "start": "the moon"})
    assert bad.status_code == 404


@pytest.mark.skipif(not (ROOT / "cache" / "models" / f"{REAL}.json").exists(), reason="real scan not cached")
def test_real_building_entrance_to_kitchen(tmp_path):
    from nudge.pdr import _setup
    nav, tr = _setup(REAL, "Kitchen", "entrance", "blind")
    if tr.corr is None:
        tr.calibrate(0.0)
    walk = tmp_path / "walk.jsonl"
    make_walk(tr, walk)
    events = []
    for line in walk.read_text().splitlines():
        rec = json.loads(line)
        out = tr.step(rec["steps"], rec["heading_deg"], now=rec["t"])
        events += out["events"]
        if out["done"]:
            break
    assert nav.done, [e["text"] for e in events]
    assert nav.reroutes == 0
