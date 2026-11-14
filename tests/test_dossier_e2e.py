"""the dossier pipeline end to end through real deep agents machinery, with a scripted model (no network, no llm).

lead -> hazard-scout (skill read, geometry, look, write) -> lead merges, validates, audits, briefs,
then tries to overwrite its own instructions and is denied.
"""
import json
import shutil

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from nudge import perceive, walkgraph
from nudge.agents import tools as T
from nudge.agents.dossier import HERE, create_dossier_agent
from nudge.navigator import Navigator
from nudge.router import Profile, plan
from nudge.sim import walk
from tests.synthetic import building

SID = "synthetic"
AGENTS_MD = HERE / "AGENTS.md"


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


@pytest.fixture(autouse=True)
def clean():
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()


def call(i, name, **args):
    return {"id": f"c{i}_{name}", "name": name, "args": args}


def ai(*calls, text=""):
    return AIMessage(content=text, tool_calls=list(calls))


def script(cand):
    """one ordered script: the subagent's model calls happen inside the lead's task() call."""
    # radius 1.0 = perceive's walk-edge threshold, so the edge it was found on (on_edges) gets tagged.
    # the 0.6 default in apply_annotations misses this sconce (0.73 m from L3-L6)
    hazard = {"x": cand["x"], "y": cand["y"], "floor": "f1", "radius": 1.0, "kind": "protruding_object",
              "note": "wall sconce at head height", "z_low_m": cand["z_low_m"], "z_high_m": cand["z_high_m"],
              "confidence": 0.9,
              "evidence": {"pano_id": cand["nearest_pano"], "bearing_deg": cand["bearing_deg"],
                           "candidate_id": cand["id"]}}
    hazards_file = {"floor": "f1", "hazards": [hazard], "rejected": [], "unchecked": []}
    annotations = {"nodes": {"p_O1": {"landmarks": [{"text": "carpet changes to tile as you enter the office",
                                                      "sense": "underfoot", "pano_id": "p_O1", "bearing_deg": 0}]}},
                   "edges": {}, "hazards": [hazard]}
    todos = [{"content": "overview", "status": "completed"},
             {"content": "hazards on f1", "status": "in_progress"},
             {"content": "merge, validate, audit, brief", "status": "pending"}]
    return [
        # lead
        ai(call(1, "building_overview", sid=SID), call(1, "write_todos", todos=todos)),
        ai(call(2, "task", subagent_type="hazard-scout", description="floor f1 of synthetic")),
        # hazard-scout
        ai(call(3, "read_file", file_path="/agent/skills/hazard-taxonomy/SKILL.md"),
           call(3, "overhang_candidates", sid=SID, floor="f1")),
        ai(call(4, "look", sid=SID, pano_id=cand["nearest_pano"], yaw_deg=cand["bearing_deg"])),
        ai(call(5, "write_file", file_path="/dossier/hazards/f1.json", content=json.dumps(hazards_file))),
        AIMessage(content="Confirmed 1 hazard on f1: wall sconce. Wrote /dossier/hazards/f1.json."),
        # lead again
        ai(call(6, "write_file", file_path="/dossier/annotations.json", content=json.dumps(annotations))),
        ai(call(7, "validate_annotations", sid=SID), call(7, "audit_routes", sid=SID, profile="blind")),
        ai(call(8, "write_file", file_path="/dossier/briefing.md",
                content="Two floors. A wall sconce sticks out at head height near the lobby. Elevator by the office.")),
        ai(call(9, "write_file", file_path="/agent/AGENTS.md", content="pwned")),
        AIMessage(content="Dossier complete."),
    ]


def run_agent(msgs):
    agent = create_dossier_agent(SID, model=ToolFake(messages=iter(msgs)))
    cfg = {"configurable": {"thread_id": "e2e"}, "recursion_limit": 200}
    tool_msgs = []
    for ns, update in agent.stream({"messages": [{"role": "user", "content": "build the dossier"}]}, config=cfg,
                                   stream_mode="updates", subgraphs=True):
        for val in (update or {}).values():
            for m in (val or {}).get("messages", []) if isinstance(val, dict) else []:
                if isinstance(m, ToolMessage):
                    tool_msgs.append((ns, m))
    return agent, cfg, tool_msgs


def test_dossier_end_to_end():
    agents_md_before = AGENTS_MD.read_text(encoding="utf-8")
    cand = perceive.overhang_candidates(SID, "f1")[0]
    agent, cfg, tool_msgs = run_agent(script(cand))
    by_name = {}
    for ns, m in tool_msgs:
        by_name.setdefault(m.name, []).append((ns, m))

    # subagent ran in its own namespace and read the routed, read-only skill
    sk_ns, sk = by_name["read_file"][0]
    assert sk_ns, "read_file should run inside the hazard-scout subgraph"
    assert "307" in str(sk.content), "the ADA hazard skill should be readable through /agent/"

    # look()'s image survives into the subagent's history as an image block
    look_ns, look_msg = by_name["look"][0]
    assert look_ns and isinstance(look_msg.content, list)
    assert [b.get("type") for b in look_msg.content] == ["text", "image"]
    assert look_msg.content[1].get("base64")

    # files on disk
    d = T.dossier_dir(SID)
    assert (d / "hazards" / "f1.json").exists() and (d / "annotations.json").exists()
    assert (d / "briefing.md").read_text(encoding="utf-8").startswith("Two floors")
    val = json.loads(by_name["validate_annotations"][0][1].content)
    assert val["ok"] and val["hazards"] == 1 and val["landmarked_panos"] == 1
    assert T.validate(SID)["ok"]

    # the agent couldn't rewrite its own instructions
    assert AGENTS_MD.read_text(encoding="utf-8") == agents_md_before
    denied = [m for _, m in by_name["write_file"] if "AGENTS" in str(m.content) or "denied" in str(m.content).lower()
              or "permission" in str(m.content).lower()]
    assert denied, [m.content for _, m in by_name["write_file"]]

    # the lead finished
    final = agent.get_state(cfg).values["messages"][-1]
    assert final.content == "Dossier complete."

    # router consumes it: edge near the sconce is a hazard
    ann = json.loads((d / "annotations.json").read_text(encoding="utf-8"))
    g0 = walkgraph.build(building())
    g = walkgraph.build(building(), ann)
    hz = [e for e in g.edges() if "hazard" in e.tags]
    assert hz, "an edge should pass within the sconce radius"
    e = hz[0]

    # blind route along that edge now costs more, or avoids it
    base = plan(g0, e.a, e.b, "blind")
    now = plan(g, e.a, e.b, "blind")
    assert now.cost > base.cost
    if now.nodes == base.nodes:
        assert now.cost == pytest.approx(base.cost + 40)

    # a walk straight past it gets warned
    straight = Profile("straight")
    r = plan(g, e.a, e.b, straight)
    nav = Navigator(g, e.b, straight)
    a = g.nodes[e.a]
    events = nav.start(a.x, a.y, a.floor)
    for p in walk(g, r, step=0.3):
        events += nav.update(*p)
    assert any(ev["type"] == "hazard" and "sconce" in ev["text"] for ev in events), events
