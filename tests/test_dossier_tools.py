import json
import shutil

import pytest

from nudge.agents import tools as T

SID = "synthetic"


@pytest.fixture(autouse=True)
def clean_dossier():
    d = T.dossier_dir(SID)
    shutil.rmtree(d, ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    shutil.rmtree(d, ignore_errors=True)
    T._graph_cached.cache_clear()


def write_ann(ann):
    (T.dossier_dir(SID) / "annotations.json").write_text(json.dumps(ann), encoding="utf-8")


def call(t, **kw):
    out = t.invoke(kw)
    return json.loads(out) if isinstance(out, str) else out


def test_overview_and_panos():
    ov = call(T.building_overview, sid=SID)
    assert [f["id"] for f in ov["floors"]] == ["f1", "f2"]
    assert ov["floors"][0]["rooms"]["Kitchen"] == 3 and ov["annotations"] == "none yet"
    ps = call(T.list_panos, sid=SID, room="kitchen")
    assert ps["count"] == 3 and all(p["room"] == "Kitchen" for p in ps["panos"])
    k1 = next(p for p in ps["panos"] if p["label"] == "K1")
    # k1 (6.5, 7) has the lobby pano l6 (6.5, 5.2) straight down -y through the door: bearing -90
    assert any(n["pano_id"] == "p_L6" and n["bearing_deg"] == -90 for n in k1["neighbours"])


def test_audit_without_annotations():
    rep = call(T.audit_routes, sid=SID, profile="wheelchair")
    assert rep["start"]["room"] == "Lobby"
    assert rep["unreachable"] == [] and rep["reachable"] == rep["trips"] == 7  # office, kitchen, conference, stairs x2, elevator x2
    assert rep["turn_points_without_landmarks"], "nothing is landmarked yet"


def test_validate_catches_bad_annotations():
    write_ann({"nodes": {"nope": {"landmarks": [{"text": "x", "sense": "sight"}]}},
               "edges": {"p_L3|p_K2": {"tags": ["narrow"]}},
               "hazards": [{"x": 1, "y": 1, "floor": "f9", "kind": "ghost", "note": "boo"}]})
    rep = call(T.validate_annotations, sid=SID)
    assert not rep["ok"]
    text = " ".join(rep["errors"])
    for frag in ("not a pano id", "not a walk-graph edge", "floor 'f9'", "kind 'ghost'"):
        assert frag in text, (frag, rep["errors"])


def test_valid_annotations_change_routes_and_audit():
    # a confirmed sconce right at the lobby->office doorway; blind route should shift to the other doorway
    write_ann({"nodes": {"p_O1": {"landmarks": [{"text": "carpet changes to tile as you enter the office",
                                                  "sense": "underfoot"}]}},
               "hazards": [{"x": 8.0, "y": 2.5, "floor": "f1", "radius": 0.4, "kind": "protruding_object",
                            "note": "wall sconce at head height on your left", "z_low_m": 1.2, "z_high_m": 1.5,
                            "confidence": 0.9, "evidence": {"pano_id": "p_L4", "bearing_deg": 0}}]})
    rep = call(T.validate_annotations, sid=SID)
    assert rep["ok"], rep
    assert rep["hazards"] == 1 and rep["landmarked_panos"] == 1 and rep["edges_marked_hazard"] >= 1
    audit = call(T.audit_routes, sid=SID, profile="blind")
    assert all(p["pano_id"] != "p_O1" for p in audit["turn_points_without_landmarks"])


def test_costar_synthetic():
    rec = call(T.costar_record, sid=SID)
    assert rec["year_built"] == 1984


def test_agent_wires_up():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    from nudge.agents.dossier import SUBAGENTS, create_dossier_agent
    agent = create_dossier_agent(SID, model=Fake(messages=iter([AIMessage("done")])))
    names = set(agent.get_graph().nodes)
    assert "model" in " ".join(names) or names
    out = agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                       config={"configurable": {"thread_id": "t"}})
    assert out["messages"][-1].content == "done"
    assert set(SUBAGENTS) == {"place-indexer", "hazard-scout", "landmark-writer", "site-surveyor", "building-cartographer"}
