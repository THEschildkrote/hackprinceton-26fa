"""explorer agent: unmapped / no-dossier / multi-scan handling, through tools that never hand out geometry."""
import json
import math
import shutil

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nudge import building as B
from nudge import walkgraph
from nudge.agents import tools as T
from nudge.agents.explorer import approve, create_explorer_agent, handle_event, make_tools, new_thread
from nudge.instruct import maneuvers
from nudge.navigator import Navigator
from tests.synthetic import building


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


@pytest.fixture(autouse=True)
def clean():
    for s in ("synthetic", "synthetic-lobby"):
        shutil.rmtree(T.dossier_dir(s), ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    for s in ("synthetic", "synthetic-lobby"):
        shutil.rmtree(T.dossier_dir(s), ignore_errors=True)
    T._graph_cached.cache_clear()


def tools_for(nav, g, runs=None, sid="synthetic"):
    def runner(s, focus, goal):
        runs.append((s, focus, goal))
    return {t.name: t for t in make_tools(nav, g, runner if runs is not None else None, sid)}


def call(t, name, **args):
    return json.loads(t[name].invoke(args))


def test_status_when_off_the_map():
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    nav.start(1.0, 3.0, "f1", math.pi)
    nav.update(-1.0, 3.0, "f1", math.pi)
    t = tools_for(nav, g)
    st = call(t, "explorer_status")
    assert st["unmapped"] and st["can_return"] and st["way_back_m"] == 2.0 and st["last_room_on_map"] == "Lobby"
    ds = call(t, "dossier_status", sid="synthetic")
    assert ds == {"sid": "synthetic", "annotations": False, "error": None, "hazards": 0, "landmarked_panos": 0,
                  "briefing": False, "portals": False}


def test_quick_survey_hands_the_runner_only_route_panos():
    g = walkgraph.build(building())
    nav = Navigator(g, "Kitchen", "blind")
    runs = []
    t = tools_for(nav, g, runs)
    out = call(t, "quick_survey", sid="synthetic", goal="Kitchen")
    assert out["started"] and runs and runs[0][0] == "synthetic" and runs[0][2] == "Kitchen"
    focus = runs[0][1]
    assert set(focus) == set(out["focus"]) and "p_K1" in focus
    assert "p_C1" not in focus and "p_O3" not in focus  # floor 2 and the far office aren't on the route


def test_commit_portal_from_candidates_joins_scans():
    graphs = {s: B.scan_graph(s) for s in ("synthetic-lobby", "synthetic")}
    g = B.merge(graphs, [])  # two scans, nothing confirmed yet
    nav = Navigator(g, "Conference Room", "wheelchair")
    ev = nav.start(2.0, 1.5, "g", 0.0, sid="synthetic-lobby")
    assert ev[0]["type"] == "no_route"
    t = tools_for(nav, g, sid="synthetic-lobby")
    cands = call(t, "scan_portal_candidates", sid="synthetic-lobby")["candidates"]
    el = next(c for c in cands if c["kind"] == "elevator" and c["b"].endswith("p_E1") or
              c["kind"] == "elevator" and c["a"].endswith("p_E1"))
    assert el["committable_now"]
    # only candidate ids, never free-form links
    assert not call(t, "commit_plan", portal_id="pc99")["ok"]
    assert not call(t, "commit_plan", goal="Kitchen", block_node="synthetic:p_L4")["ok"]
    out = call(t, "commit_plan", portal_id=el["id"], reason="Joining the lobby elevator to the office floors.")
    assert out["ok"], out
    e = g.edge(el["a"], el["b"])
    assert {"portal", "uncertain", "elevator"} <= e.tags
    assert any(g.edge(a, b) is e for a, b in zip(nav.route.nodes, nav.route.nodes[1:]))
    assert any(m["type"] == "floor" and m["portal"] for m in maneuvers(g, nav.route))


def test_commit_goal_while_unmapped_waits_for_the_map():
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    nav.start(1.0, 3.0, "f1", math.pi)
    nav.update(-1.0, 3.0, "f1", math.pi)
    t = tools_for(nav, g)
    out = call(t, "commit_plan", goal="Kitchen")
    assert out["ok"] and nav.goal == "Kitchen" and "back on the map" in out["note"]
    ev = nav.update(1.0, 3.0, "f1", 0.0)
    assert [e["type"] for e in ev][:2] == ["remapped", "reroute"]
    assert g.nodes[nav.route.nodes[-1]].room_label == "Kitchen"


def scripted(*turns):
    msgs = []
    for i, tc in enumerate(turns):
        if isinstance(tc, str):
            msgs.append(AIMessage(content=tc))
        else:
            name, args, text = tc
            msgs.append(AIMessage(content=text, tool_calls=[{"id": f"x{i}", "name": name, "args": args}]))
    return ToolFake(messages=iter(msgs))


def test_agent_plans_surveys_and_waits_for_approval():
    g = walkgraph.build(building())
    nav = Navigator(g, "Office", "blind")
    nav.start(1.0, 3.0, "f1", math.pi)
    runs = []
    model = scripted(
        ("explorer_status", {}, ""),
        ("write_todos", {"todos": [{"content": "survey route to kitchen", "status": "in_progress"},
                                   {"content": "switch goal", "status": "pending"}]}, ""),
        ("quick_survey", {"sid": "synthetic", "goal": "Kitchen"}, ""),
        ("commit_plan", {"goal": "Kitchen", "reason": "The kitchen route is being surveyed now."},
         "I'm checking the route to the kitchen for hazards. Shall I take you there instead?"),
        "Okay, heading to the kitchen.",
    )
    agent = create_explorer_agent(nav, g, sid="synthetic", model=model,
                                  survey_runner=lambda s, f, goal: runs.append((s, f, goal)))
    tid = new_thread()
    out = handle_event(agent, tid, "I'd rather go to the kitchen; it has never been surveyed.")
    assert out["pending"] and out["pending"]["name"] == "commit_plan"
    assert runs and runs[0][2] == "Kitchen"
    assert nav.goal == "Office"  # nothing changed before approval
    done = approve(agent, tid, "approve")
    assert done["pending"] is None and nav.goal == "Kitchen"
    assert g.nodes[nav.route.nodes[-1]].room_label == "Kitchen"
