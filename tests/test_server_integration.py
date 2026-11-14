"""endpoints added when task a, the outdoor leg and the mobile app came together."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from nudge import building as B
from nudge.server import app

c = TestClient(app)
SIDS = ("synthetic-lobby", "synthetic")


@pytest.fixture
def portal():
    for s in SIDS:
        shutil.rmtree(B.DOSSIERS / s, ignore_errors=True)
    graphs = {s: B.scan_graph(s) for s in SIDS}
    p = next(x for x in B.candidates_portals(list(graphs), graphs) if x["b"] == "synthetic:p_E1")
    d = B.DOSSIERS / "synthetic"
    d.mkdir(parents=True, exist_ok=True)
    (d / "portals.json").write_text(json.dumps({"portals": [{**p, "confirmed": True}]}), encoding="utf-8")
    yield p
    for s in SIDS:
        shutil.rmtree(B.DOSSIERS / s, ignore_errors=True)


def test_cors_for_the_expo_web_build():
    r = c.options("/api/profiles", headers={"Origin": "http://localhost:8081", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://localhost:8081")


def test_building_info_lists_sibling_scans():
    r = c.get("/api/building/synthetic-lobby").json()
    sids = {m["sid"] if isinstance(m, dict) else m for m in r["models"]}
    assert set(SIDS) <= sids and r["candidates"]


def test_nav_across_scans_from_the_street_lobby(portal):
    v1 = B.scan_graph("synthetic-lobby").nodes["p_V1"]
    s = c.post("/api/nav/start", json={"sid": "synthetic-lobby", "building": True, "to": "Conference Room",
                                       "profile": "wheelchair", "x": v1.x, "y": v1.y, "floor": "g"}).json()
    assert s["state"]["route"]["length"] > 0 and s["device"]["line"]
    texts = " ".join(m["text"] for m in s["state"]["maneuvers"])
    assert "elevator" in texts.lower() and "meter" in texts
    lines = c.get(f"/api/device/{s['session']}/route").json()["lines"]
    assert lines and all(len(x.encode()) < 20 for x in lines)


def test_unmapped_update_hints_the_explorer():
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Kitchen", "profile": "blind",
                                       "x": 2, "y": 1.5, "floor": "f1"}).json()
    u = None
    for _ in range(3):  # far outside the scan
        u = c.post("/api/nav/update", json={"session": s["session"], "x": 60, "y": 60, "floor": "f1"}).json()
    kinds = [e["type"] for e in u["events"]]
    assert "explore_hint" in u or "unmapped" in kinds or u["state"].get("unmapped"), u


def test_explore_without_session_is_404():
    assert c.post("/api/nav/explore/decide", json={"session": "nope"}).status_code == 404


def test_outdoor_site_without_keys_is_a_clear_error(monkeypatch):
    from nudge import handoff

    def boom(sid, refresh=False):
        raise RuntimeError("VEXCEL_API_KEY is not set")
    monkeypatch.setattr(handoff, "load_site", boom)
    r = c.get("/api/outdoor/synthetic/site")
    assert r.status_code == 502 and "Vexcel" in r.json()["detail"]
