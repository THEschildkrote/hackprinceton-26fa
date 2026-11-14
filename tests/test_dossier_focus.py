"""route-scoped quick dossier for a first visit, through dossier.run(focus=...) and a scripted model."""
import json
import shutil

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nudge import perceive, walkgraph
from nudge.agents import tools as T
from nudge.agents.dossier import kickoff, run
from tests.synthetic import building

SID = "synthetic"


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


def ai(*calls, text=""):
    return AIMessage(content=text, tool_calls=list(calls))


def c(i, name, **args):
    return {"id": f"q{i}_{name}", "name": name, "args": args}


def test_kickoff_variants():
    assert "QUICK SURVEY FIRST" in kickoff(SID, ["p_L5", "p_K1"], "Kitchen") and "p_L5,p_K1" in kickoff(SID, ["p_L5", "p_K1"])
    assert "QUICK SURVEY" not in kickoff(SID) and "building_models" in kickoff(SID)


def test_focus_filters_geometry_to_the_route():
    rf = T.route_focus(SID, "Kitchen")
    near = json.loads(T.overhang_candidates.invoke({"sid": SID, "floor": "f1", "panos": ",".join(rf["focus"])}))
    allc = json.loads(T.overhang_candidates.invoke({"sid": SID, "floor": "f1"}))
    far = json.loads(T.overhang_candidates.invoke({"sid": SID, "floor": "f1", "panos": "p_O3"}))
    assert near == allc and far == []  # the planted sconce is by the kitchen door, not by the office


def test_quick_dossier_run_end_to_end():
    rf = T.route_focus(SID, "Kitchen")
    focus = rf["focus"]
    cand = perceive.overhang_candidates(SID, "f1")[0]
    hazard = {"x": cand["x"], "y": cand["y"], "floor": "f1", "radius": 0.6, "kind": "protruding_object",
              "note": "wall sconce at head height", "on_edges": cand["on_edges"], "confidence": 0.9,
              "evidence": {"pano_id": cand["nearest_pano"], "bearing_deg": cand["bearing_deg"], "candidate_id": cand["id"]}}
    script = [
        ai(c(1, "write_todos", todos=[{"content": "route hazards", "status": "in_progress"}])),
        ai(c(2, "task", subagent_type="hazard-scout", description=f"synthetic floor f1, panos {','.join(focus)}")),
        ai(c(3, "overhang_candidates", sid=SID, floor="f1", panos=",".join(focus))),
        ai(c(4, "look", sid=SID, pano_id=cand["nearest_pano"], yaw_deg=cand["bearing_deg"])),
        ai(c(5, "write_file", file_path="/dossier/hazards/f1.json",
             content=json.dumps({"floor": "f1", "hazards": [hazard], "rejected": [], "unchecked": []}))),
        AIMessage(content="1 hazard on the route."),
        ai(c(6, "write_file", file_path="/dossier/annotations.json", content=json.dumps({"hazards": [hazard]}))),
        ai(c(7, "validate_annotations", sid=SID)),
        ai(c(8, "write_file", file_path="/dossier/briefing.md", content="Head-height sconce by the kitchen door.")),
        AIMessage(content="Quick survey done; continuing with the full survey later."),
    ]
    events = []
    out = run(SID, model=ToolFake(messages=iter(script)), on_event=events.append, focus=focus, goal="Kitchen")
    assert (out / "annotations.json").exists() and (out / "hazards" / "f1.json").exists()
    assert T.validate(SID)["ok"]
    assert any("-> hazard-scout" in e for e in events)
    assert any(e.startswith("[") and "overhang_candidates(floor=f1, panos=" in e for e in events)
    # the router now avoids it on that route: the hazard tags the kitchen-side link it was found on
    g = walkgraph.build(building(), json.loads((out / "annotations.json").read_text(encoding="utf-8")))
    a, b = cand["on_edges"][0]
    assert "hazard" in g.edge(a, b).tags
