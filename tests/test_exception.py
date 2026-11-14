import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nudge import walkgraph
from nudge.agents.exception import (approve, create_exception_agent, handle_event, make_tools, new_thread)
from nudge.navigator import Navigator
from tests.synthetic import building


class ToolFake(GenericFakeChatModel):
    """scripted chat model that pretends to support tool calling."""

    def bind_tools(self, tools, **kw):
        return self


@pytest.fixture
def g():
    return walkgraph.build(building())


def session(g, profile="blind", goal="Conference Room", at="p_O4"):
    nav = Navigator(g, goal, profile)
    n = g.nodes[at]
    nav.start(n.x, n.y, n.floor)
    return nav, {t.name: t for t in make_tools(nav, g)}


def call(tools, name, **args):
    return json.loads(tools[name].invoke(args))


def test_nav_status(g):
    nav, t = session(g)
    s = call(t, "nav_status")
    assert s["at"]["room"] == "Office" and s["at"]["nearest_pano"] == "O4"
    assert s["goal"] == "Conference Room" and s["profile"] == "blind"
    assert s["remaining_m"] > 0 and s["blocked_nodes"] == [] and s["reroutes"] == 0


def test_list_nearby(g):
    _, t = session(g)
    near = call(t, "list_nearby", radius_m=6)
    kinds = {c["kind"] for c in near["connections"]}
    assert "elevator" in kinds and "Office" in near["rooms"]
    assert near["panos"][0]["pano"] == "O4"


def test_compare_options_elevator_out(g):
    nav, t = session(g)
    before = (list(nav.route.nodes), set(nav.blocked_nodes))
    res = call(t, "compare_options", block_node="E1")
    assert "no_route" in res["wheelchair"]
    assert "S1" in json.dumps(res["blind"]) or res["blind"]["floor_changes"][0]["via"] == "stairs"
    assert res["blind"]["floor_changes"][0]["via"] == "stairs"
    # what-if only: live plan untouched
    assert (list(nav.route.nodes), set(nav.blocked_nodes)) == before
    base = call(t, "compare_options")
    assert base["wheelchair"]["floor_changes"][0]["via"] == "elevator"


def test_compare_options_locked_door(g):
    nav, t = session(g, at="p_L4", goal="Office")
    res = call(t, "compare_options", block_edge="L4|O1", profiles="blind")
    assert res["blind"]["length_m"] > 0
    assert "error" in call(t, "compare_options", block_edge="L1|K3")


def test_alternate_goals(g):
    _, t = session(g, at="p_L1", goal="Kitchen")
    res = call(t, "alternate_goals", query="exit")
    assert res["reachable"][0]["room"] == "Lobby"
    res = call(t, "alternate_goals", query="stairs")
    assert any(r["room"] == "Stairs" for r in res["reachable"])


def test_commit_reroute_mutates(g):
    nav, t = session(g)
    res = call(t, "commit_reroute", block_node="E1", reason="The elevator is out of service.")
    assert res["ok"] and "p_E1" in nav.blocked_nodes and "p_S1" in nav.route.nodes
    assert res["events"][0].startswith("The elevator is out of service.")
    nav_w, tw = session(g, profile="wheelchair")
    res = call(tw, "commit_reroute", block_node="E1", reason="The elevator is out of service.")
    assert not res["ok"]  # no stairs for a wheelchair, so no route
    res = call(t, "commit_reroute", goal="Kitchen")
    assert res["ok"] and nav.goal == "Kitchen"


def scripted(*turns):
    msgs = []
    for i, tc in enumerate(turns):
        if isinstance(tc, str):
            msgs.append(AIMessage(content=tc))
        else:
            name, args, text = tc
            msgs.append(AIMessage(content=text, tool_calls=[{"id": f"c{i}", "name": name, "args": args}]))
    return ToolFake(messages=iter(msgs))


def script():
    return scripted(
        ("compare_options", {"block_node": "E1"}, ""),
        ("commit_reroute", {"block_node": "E1", "reason": "The elevator is out of service."},
         "The elevator is out. The stairs are 20 feet away. Shall I switch your route to the stairs?"),
        "Okay. Your new route uses the stairs.",
    )


def test_agent_pauses_then_approve_applies(g):
    nav, _ = session(g)
    agent = create_exception_agent(nav, g, model=script())
    tid = new_thread()
    out = handle_event(agent, tid, "Elevator E1 is out of service.")
    assert out["pending"] and out["pending"]["name"] == "commit_reroute"
    assert any("stairs" in s for s in out["say"])
    assert "p_E1" not in nav.blocked_nodes  # nothing changed before approval
    done = approve(agent, tid, "approve")
    assert done["pending"] is None
    assert "p_E1" in nav.blocked_nodes and "p_S1" in nav.route.nodes
    assert done["say"] and "stairs" in done["say"][-1]


def test_agent_reject_leaves_plan(g):
    nav, _ = session(g)
    route = list(nav.route.nodes)
    agent = create_exception_agent(nav, g, model=script())
    tid = new_thread()
    handle_event(agent, tid, "Elevator E1 is out of service.")
    approve(agent, tid, "reject", message="I'd rather wait for the elevator.")
    assert "p_E1" not in nav.blocked_nodes and nav.route.nodes == route
