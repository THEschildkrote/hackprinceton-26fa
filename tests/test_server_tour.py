import pytest
from fastapi.testclient import TestClient

from nudge import server
from nudge.agents import llm


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MATTERPORT_SDK_KEY", raising=False)
    import nudge.mp as mp
    real = mp.secret
    monkeypatch.setattr(mp, "secret", lambda name, required=True: None if name == "MATTERPORT_SDK_KEY" else real(name, required))
    # no model in tests: the exception endpoint must take the deterministic path
    monkeypatch.setattr(llm, "has_credentials", lambda s=None: False)
    return TestClient(server.app)


def start(client, at=(2, 11), to="Conference Room", profile="blind"):
    r = client.post("/api/nav/start", json={"sid": "synthetic", "to": to, "profile": profile,
                                            "x": at[0], "y": at[1], "floor": "f1", "heading": 0.0})
    assert r.status_code == 200, r.text
    return r.json()


def test_config_without_key(client):
    assert client.get("/api/config").json() == {"matterport_sdk_key": None}


def test_look_returns_jpeg(client):
    r = client.get("/api/look/synthetic", params={"x": 2, "y": 11, "floor": "f1", "yaw": 90, "size": 256})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8" and r.headers["x-pano"] == "p_K3"
    assert client.get("/api/look/synthetic", params={"pano": "p_L4", "yaw": 0}).status_code == 200
    assert client.get("/api/look/synthetic").status_code == 400


def test_nav_update_has_device_line(client):
    s = start(client)
    assert s["device"]["line"].startswith("N,")
    r = client.post("/api/nav/update", json={"session": s["session"], "x": 3, "y": 10, "floor": "f1",
                                              "heading": -0.8}).json()
    line = r["device"]["line"]
    assert line.startswith("N,") and len(line.encode()) < 20
    assert r["device"]["packet"]["m_next"] is not None


def test_exception_fallback_blocks_elevator_and_reroutes(client):
    s = start(client)
    sid = s["session"]
    nav = server.get_navigator(sid)
    assert "p_E1" in nav.route.nodes, "blind route starts via the elevator"
    r = client.post("/api/nav/exception", json={"session": sid, "text": "the elevator is out of service"}).json()
    assert r["mode"] == "fallback" and r["pending"] is None
    assert "p_E1" in nav.blocked_nodes and "p_S1" in nav.route.nodes
    assert any("elevator" in (e.get("text") or "").lower() for e in r["events"])
    assert r["events"][0]["type"] == "reroute"
    assert r["device"]["line"].startswith("N,")


def test_exception_fallback_locked_door(client):
    s = start(client, at=(5, 1.5), to="Office")
    nav = server.get_navigator(s["session"])
    first_door = next((a, b) for a, b in zip(nav.route.nodes, nav.route.nodes[1:]) if "door" in nav.g.edge(a, b).tags)
    r = client.post("/api/nav/exception", json={"session": s["session"], "text": "this door is locked"}).json()
    assert r["mode"] == "fallback"
    assert first_door in nav.blocked_edges
    pairs = set(zip(nav.route.nodes, nav.route.nodes[1:]))
    assert first_door not in pairs and first_door[::-1] not in pairs


def test_registry_is_importable(client):
    s = start(client)
    assert server.get_navigator(s["session"]) is server.sessions[s["session"]]
    assert server.get_navigator("nope") is None


def test_elevator_out_while_standing_in_it(client):
    s = start(client)
    nav = server.get_navigator(s["session"])
    e1 = nav.g.nodes["p_E1"]
    client.post("/api/nav/update", json={"session": s["session"], "x": e1.x, "y": e1.y - 0.1, "floor": "f1"})
    r = client.post("/api/nav/exception", json={"session": s["session"], "text": "elevator is out"}).json()
    assert r["events"][0]["type"] == "reroute", r["events"]
    assert "p_S1" in nav.route.nodes and "p_E1" not in nav.route.nodes
