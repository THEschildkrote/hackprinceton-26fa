"""the ops dashboard feed: the trace bus, langchain callbacks into it, and the snapshot / replay endpoints."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from nudge import traces, walkgraph
from nudge.agents.exception import create_exception_agent, handle_event, new_thread
from tests.synthetic import building
from tests.test_exception import script, session


@pytest.fixture(autouse=True)
def clean(tmp_path):
    from nudge import hub
    hub.reset(tmp_path / "hub.json")   # never the real cache/hub.json, which a running server may hold open
    traces.reset()
    yield
    traces.reset()
    hub.reset()


# --- the bus ----------------------------------------------------------------------------------------

def test_publish_subscribe_and_unsubscribe():
    got = []
    off = traces.subscribe(got.append)
    a = traces.publish("step", session="s1", steps=2, heading_deg=None)
    b = traces.publish("position", session="s2", x=1.0)
    assert [e["kind"] for e in got] == ["step", "position"]
    assert b["id"] > a["id"] and "heading_deg" not in a      # none fields are dropped
    off()
    traces.publish("step", session="s1")
    assert len(got) == 2
    assert [e["id"] for e in traces.recent(since=a["id"])] == [b["id"], b["id"] + 1]
    assert {e["session"] for e in traces.recent(session="s1")} == {"s1"}


def test_broken_listener_never_breaks_publish():
    traces.subscribe(lambda ev: 1 / 0)
    assert traces.publish("band", cue="left")["cue"] == "left"


def test_ring_keeps_only_the_newest():
    for i in range(traces.RING + 50):
        traces.publish("step", n=i)
    evs = traces.recent(n=10_000)
    assert len(evs) == traces.RING and evs[-1]["n"] == traces.RING + 49 and evs[0]["n"] == 50


def test_clip_truncates_and_hides_images():
    long = traces.clip("the door is locked " * 60)
    assert len(long) == traces.CLIP and long.endswith("…")
    assert traces.clip(b"\x00" * 9) == "<9 bytes>"
    assert traces.clip("iVBORw0KGgo" + "A" * 4000).startswith("<image or blob")
    assert "<image>" in traces.clip({"content": [{"type": "image", "base64": "AAAA"}, {"type": "text", "text": "hi"}]})
    assert traces.clip("the door is locked") == "the door is locked"
    assert traces.clip(None) is None


# --- langchain callbacks ------------------------------------------------------------------------------

def test_trace_handler_labels_subagents():
    h = traces.TraceHandler(agent="dossier lead", session="s9")
    root, task, inner, tool = (uuid.uuid4() for _ in range(4))
    h.on_chain_start({}, {}, run_id=root)
    h.on_chat_model_start({}, [[]], run_id=uuid.uuid4(), parent_run_id=root,
                          invocation_params={"model": "models/gemini-3.5-flash-lite"})
    h.on_tool_start({"name": "task"}, "", run_id=task, parent_run_id=root,
                    inputs={"subagent_type": "room-namer", "description": "name the rooms on floor 2"})
    h.on_chat_model_start({}, [[]], run_id=inner, parent_run_id=task)
    msg = AIMessage(content="", tool_calls=[{"id": "1", "name": "look_at_pano", "args": {}}],
                    usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128})
    h.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=inner)
    h.on_tool_start({"name": "look_at_pano"}, "", run_id=tool, parent_run_id=inner, inputs={"pano": "p1"})
    h.on_tool_end("a hallway with a door", run_id=tool)
    h.on_tool_end("floor 2: seminar room, lounge", run_id=task)
    evs = [e for e in traces.recent() if e["kind"] == "agent"]
    seq = [(e["event"], e["agent"], e["depth"]) for e in evs]
    assert seq == [("model_start", "dossier lead", 0), ("delegate", "dossier lead", 0),
                   ("model_start", "room-namer", 1), ("model_end", "room-namer", 1),
                   ("tool_start", "room-namer", 1), ("tool_end", "room-namer", 1),
                   ("returned", "dossier lead", 0)]
    assert evs[0]["model"] == "gemini-3.5-flash-lite" and evs[0]["session"] == "s9"
    assert evs[1]["subagent"] == "room-namer" and "floor 2" in evs[1]["args"]
    assert evs[3]["tool_calls"] == ["look_at_pano"] and evs[3]["tokens_in"] == 120
    assert evs[4]["run"] == evs[5]["run"] and evs[5]["ms"] is not None
    assert evs[4]["run"] == str(tool) and evs[2]["run"] != evs[4]["run"]   # whole ids: prefixes collide


def test_exception_agent_run_is_traced():
    g = walkgraph.build(building())
    nav, _ = session(g)
    agent = create_exception_agent(nav, g, model=script())
    out = handle_event(agent, new_thread(), "Elevator E1 is out of service.")
    assert out["pending"]["name"] == "commit_reroute"
    evs = [e for e in traces.recent() if e["kind"] == "agent"]
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "event" and "model_start" in kinds and "model_end" in kinds
    assert any(e["event"] == "tool_start" and e["tool"] == "compare_options" for e in evs)
    assert any(e["event"] == "tool_end" for e in evs)
    pend = [e for e in evs if e["event"] == "hitl_pending"]
    assert pend and pend[0]["tool"] == "commit_reroute"
    assert any(e["event"] == "say" and "stairs" in e["text"] for e in evs)
    assert {e["agent"] for e in evs} == {"exception agent"}


# --- endpoints ----------------------------------------------------------------------------------------

from nudge import server  # noqa: E402

c = TestClient(server.app)


def test_snapshot_shape_and_walk_traces():
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Conference Room", "profile": "blind",
                                       "x": 2.0, "y": 11.0, "floor": "f1"}).json()
    c.post("/api/nav/update", json={"session": s["session"], "x": 4.0, "y": 11.0, "floor": "f1"})
    snap = c.get("/api/ops/snapshot").json()
    assert {"sessions", "band", "travellers", "model", "buildings", "events"} <= set(snap)
    v = next(x for x in snap["sessions"] if x["session"] == s["session"])
    assert v["sid"] == "synthetic" and v["goal"] == "Conference Room" and v["replay"] is False
    assert v["position"]["floor"] == "f1" and v["route"]["points"] and v["remaining"] > 0
    assert "synthetic" in snap["buildings"]
    kinds = {e["kind"] for e in snap["events"] if e.get("session") == s["session"]}
    assert {"session", "position"} <= kinds
    assert c.get("/api/ops/snapshot?events=0").json()["events"] == []   # polling skips the history


def test_replay_drives_the_real_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    r = c.post("/api/ops/replay", json={"sid": "synthetic", "start": "Lobby", "to": "Conference Room",
                                        "speed": 4.0})
    assert r.status_code == 200, r.text
    sess = r.json()["session"]
    assert r.json()["file"] == "ops_synthetic.jsonl"   # made from this session, so it fits the graph
    assert (tmp_path / "cache" / "replays" / "ops_synthetic.jsonl").exists()
    deadline = time.time() + 30
    while time.time() < deadline and not any(e["kind"] == "replay" and e.get("phase") == "end"
                                             for e in traces.recent(n=2000, session=sess)):
        time.sleep(0.1)
    evs = traces.recent(n=2000, session=sess)
    kinds = [e["kind"] for e in evs]
    assert "step" in kinds and "position" in kinds and kinds.count("replay") >= 2
    assert any(e["kind"] == "replay" and e.get("phase") == "end" and e.get("arrived") for e in evs)
    assert not any(e.get("phase") == "error" for e in evs)
    assert c.post("/api/ops/replay/stop").json()["stopped"] == 0
    v = next(x for x in c.get("/api/ops/snapshot?events=0").json()["sessions"] if x["session"] == sess)
    assert v["replay"] is True and v["replaying"] is False and v["done"]   # still marked a replay once it ends


def test_lost_compass_while_walking_free_does_not_crash():
    """phones drop the compass (stairs, magnets). in free mode that used to raise in Tracker._free."""
    from nudge.navigator import Navigator
    from nudge.pdr import Tracker
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    k3 = g.nodes["p_K3"]
    nav.start(k3.x, k3.y, k3.floor)
    tr = Tracker(nav)
    tr.calibrate(0.0)
    tr.mode, tr.free_to = "free", None
    before = tr.pos
    out = tr.step(2, None)
    assert tr.pos == before and "events" in out
