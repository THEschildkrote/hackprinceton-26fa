"""the dashboard's sign-in: open with no code set; with one, the page and its data need a session."""
import pytest
from fastapi.testclient import TestClient

from nudge import server


@pytest.fixture
def code(monkeypatch):
    monkeypatch.setattr(server, "_dash_password", lambda: "test-only-code")
    return "test-only-code"


def test_without_a_code_the_dashboard_is_open(monkeypatch):
    monkeypatch.setattr(server, "_dash_password", lambda: None)
    c = TestClient(server.app)
    assert c.get("/ops.html").status_code == 200
    assert c.get("/api/ops/events").status_code == 200
    assert c.get("/api/auth/status").json() == {"required": False, "signed_in": False}


def test_the_site_is_public_and_the_dashboard_sends_you_to_sign_in(code):
    c = TestClient(server.app)
    assert c.get("/").status_code == 200 and "Isaac" in c.get("/").text
    r = c.get("/ops.html", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login.html?next=/ops.html"
    assert c.get("/api/ops/snapshot?events=0").status_code == 401
    assert c.get("/api/profiles").status_code == 200        # the apps' api is never gated


def test_wrong_code_is_refused_right_code_opens_and_sign_out_closes(code):
    c = TestClient(server.app)
    assert c.post("/api/auth/login", json={"code": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"code": code})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/status").json() == {"required": True, "signed_in": True}
    assert c.get("/ops.html").status_code == 200
    assert c.get("/api/ops/events").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/ops/events").status_code == 401


def test_a_forged_or_expired_session_is_refused(code, monkeypatch):
    c = TestClient(server.app)
    c.cookies.set(server.AUTH_COOKIE, "9999999999.deadbeef")
    assert c.get("/api/ops/events").status_code == 401
    c.cookies.set(server.AUTH_COOKIE, server._auth_token(now=0))  # expired long ago
    assert c.get("/api/ops/events").status_code == 401
