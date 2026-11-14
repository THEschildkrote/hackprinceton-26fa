import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nudge import walkgraph
from nudge.device import decode_live, decode_step
from nudge.device_api import make_router
from nudge.navigator import Navigator
from tests.synthetic import building

STEP = re.compile(r"^\d+,(?:[DTBUSWA],[LRS],-?\d+,\d+\.\d|F,[UD],[ESX],\d+)$")
LIVE = re.compile(r"^N,(\d+\.\d)?,(-?\d+)?,(?:[DTBUSWAF-])[LRS],[0-3]$")


@pytest.fixture
def client():
    g = walkgraph.build(building())
    nav = Navigator(g, "Conference Room", "blind")
    k3 = g.nodes["p_K3"]
    nav.start(k3.x, k3.y, k3.floor, heading=0.0)
    app = FastAPI()
    app.include_router(make_router(lambda s: nav if s == "s1" else None))
    c = TestClient(app)
    c.nav = nav
    return c


def test_route_lines(client):
    r = client.get("/api/device/s1/route").json()
    assert r["lines"][0].startswith("0,D,") and r["lines"][-1].split(",")[1] == "A"
    assert any(",F,U,E," in l for l in r["lines"]), r["lines"]
    for line in r["lines"]:
        assert STEP.match(line), line
        assert len(line.encode()) < 20
        assert decode_step(line)["i"] == int(line.split(",")[0])


def test_live_line_and_seq(client):
    a = client.get("/api/device/s1/live").json()
    assert LIVE.match(a["line"]) and len(a["line"].encode()) < 20, a["line"]
    assert decode_live(a["line"])["act_next"] == a["packet"]["act_next"]
    b = client.get("/api/device/s1/live").json()
    assert b["seq"] == a["seq"], "unchanged line keeps its seq"
    # walk a few metres along the route; the line changes and seq moves on
    nav = client.nav
    n1 = nav.g.nodes[nav.route.nodes[1]]
    nav.update(n1.x, n1.y, n1.floor, heading=0.0)
    c = client.get("/api/device/s1/live").json()
    assert c["line"] != a["line"] and c["seq"] == a["seq"] + 1


def test_buttons(client):
    rep = client.post("/api/device/s1/button", json={"event": "repeat"}).json()
    assert rep["say"].startswith("Walk ") and "meter" in rep["say"]
    lost = client.post("/api/device/s1/button", json={"event": "lost"}).json()
    assert "Let me find you" in lost["say"] and lost["reroutes"] == 1
    assert client.post("/api/device/s1/button", json={"event": "dance"}).status_code == 400


def test_unknown_session(client):
    assert client.get("/api/device/nope/live").status_code == 404
