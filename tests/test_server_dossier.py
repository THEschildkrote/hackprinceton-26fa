import shutil
import time

import pytest
from fastapi.testclient import TestClient

import nudge.server as S
from nudge.agents import tools as T


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(S, "DEMO_PACE_S", 0)
    shutil.rmtree(T.dossier_dir("synthetic"), ignore_errors=True)
    S._runs.clear()
    T._graph_cached.cache_clear()
    yield TestClient(S.app)
    for _ in range(100):  # let a background run finish before cleaning up
        if not any(r["status"] == "running" for r in S._runs.values()):
            break
        time.sleep(0.1)
    shutil.rmtree(T.dossier_dir("synthetic"), ignore_errors=True)
    T._graph_cached.cache_clear()


def wait(client, sid="synthetic", timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/dossier/{sid}/events").json()
        if r["status"] != "running":
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_empty_dossier(client):
    d = client.get("/api/dossier/synthetic").json()
    assert d["files"] == [] and d["annotations_json"] is None and d["briefing_md"] is None
    assert d["validation"]["ok"] is False and d["run"] is None


def test_real_run_needs_key(client, monkeypatch):
    monkeypatch.setattr(S, "_has_llm_key", lambda: False)
    r = client.post("/api/dossier/synthetic/run")
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_demo_only_synthetic(client):
    assert client.post("/api/dossier/zYdLbVFphGC/demo").status_code == 400


def test_demo_run_streams_events_and_writes_dossier(client):
    r = client.post("/api/dossier/synthetic/demo")
    assert r.status_code == 200 and r.json()["mode"] == "demo"
    done = wait(client)
    assert done["status"] == "done", done.get("error")
    ev = client.get("/api/dossier/synthetic/events?after=0").json()["events"]
    kinds = [e["kind"] for e in ev]
    assert kinds[0] == "start" and kinds[-1] == "done"
    delegated = [e["subagent"] for e in ev if e["kind"] == "delegate"]
    assert delegated == ["site-surveyor", "hazard-scout", "hazard-scout", "landmark-writer"]
    # subagent work is attributed to the subagent, not the lead
    assert {e["who"] for e in ev if e["kind"] == "look"} == {"hazard-scout", "landmark-writer"}
    assert any(e["kind"] == "plan" and all(t["status"] == "completed" for t in e["todos"]) for e in ev)
    # paging
    assert client.get(f"/api/dossier/synthetic/events?after={len(ev) - 2}").json()["events"] == ev[-2:]

    d = client.get("/api/dossier/synthetic").json()
    assert {"annotations.json", "briefing.md", "audit.md", "site.md", "hazards/f1.json", "landmarks/f1.json"} <= set(d["files"])
    assert d["validation"]["ok"] and d["validation"]["hazards"] == 2 and d["validation"]["edges_marked_hazard"] >= 1
    assert d["briefing_md"].startswith("This is a two-floor")

    # the graph endpoint now carries the dossier's hazards
    g = client.get("/api/graph/synthetic").json()
    assert any("hazard" in e["tags"] for e in g["edges"])
    assert any(n["landmarks"] for n in g["nodes"])


def test_second_run_while_running_is_409(client, monkeypatch):
    monkeypatch.setattr(S, "DEMO_PACE_S", 0.05)
    assert client.post("/api/dossier/synthetic/demo").status_code == 200
    assert client.post("/api/dossier/synthetic/demo").status_code == 409
    wait(client)
