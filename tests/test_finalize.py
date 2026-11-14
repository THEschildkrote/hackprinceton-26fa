"""the deterministic end of a dossier run: tolerant merge, valid files no matter what, gaps in audit.md."""
import json
import shutil

import pytest

from nudge import places
from nudge.agents import finalize as F
from nudge.agents import tools as T

SID = "synthetic"


@pytest.fixture(autouse=True)
def clean():
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()


def put(rel, obj):
    p = T.dossier_dir(SID) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def test_merge_fixes_sloppy_subagent_files():
    # the kind of thing free models write: string numbers, synonyms, labels instead of ids, a weak one, a bad one
    put("hazards/f1.json", {"floor": "f1", "hazards": [
        {"x": "2.73", "y": "4.8", "kind": "sconce", "note": "wall sconce at head height", "confidence": "0.9",
         "evidence": {"pano_id": "L5", "bearing_deg": 76}, "on_edges": ["p_L3|p_L6"]},
        {"kind": "glass", "note": "glass wall", "evidence": {"pano_id": "p_L6"}},          # no x/y: placed at pano
        {"x": 1, "y": 1, "kind": "other", "note": "maybe a chair", "confidence": 0.3},      # unconfirmed
        "not an object"]})
    put("landmarks/f1.json", {"floor": "f1", "landmarks": [
        {"pano_id": "O1", "text": "carpet changes to tile", "sense": "floor"},
        {"pano_id": "nope", "text": "x", "sense": "touch"}]})
    put("hazards/f2.json", "{not json")
    r = F.merge(SID)
    assert r["validate"]["ok"], r
    ann = json.loads((T.dossier_dir(SID) / "annotations.json").read_text())
    kinds = sorted(h["kind"] for h in ann["hazards"])
    assert kinds == ["glass_wall", "protruding_object"]
    sconce = next(h for h in ann["hazards"] if h["kind"] == "protruding_object")
    assert sconce["evidence"]["pano_id"] == "p_L5" and sconce["on_edges"] == [["p_L3", "p_L6"]]
    assert ann["nodes"]["p_O1"]["landmarks"][0]["sense"] == "underfoot"
    assert any("confidence 0.3" in x for x in r["dropped"]) and any("not a pano id" in x for x in r["dropped"])
    assert any("not valid json" in x for x in r["notes"])
    # and the router uses it
    g = T.graph(SID)
    assert "hazard" in g.edge("p_L3", "p_L6").tags


def test_finalize_after_a_run_that_wrote_nothing():
    out = F.finalize(SID, "budget reached")
    d = T.dossier_dir(SID)
    assert T.validate(SID)["ok"] and places.validate(SID)["ok"]
    assert (d / "briefing.md").exists() and out["briefing"] == "fallback"
    audit = (d / "audit.md").read_text()
    assert "Gaps (written automatically)" in audit and "budget reached" in audit
    assert "no hazard survey file" in audit and "turn points without a non-visual landmark" in audit


def test_finalize_keeps_good_places_and_drops_bad_ones():
    seed = places.seed(SID)["places"]
    kitchen = next(p for p in seed if p["name"] == "Kitchen")
    put("places/f1.json", {"places": [
        {"id": kitchen["id"], "aliases": ["kitchenette"], "serves": ["water"], "source": "agent"},
        {"id": "feature:tap", "name": "tap", "kind": "feature", "serves": ["water"]},  # no anchor: invalid
    ]})
    assert not places.validate(SID)["ok"]
    out = F.finalize(SID)
    assert out["places"]["ok"] and out["places_dropped"] == 1
    cat = places.load(SID)
    assert "kitchenette" in next(p for p in cat["places"] if p["id"] == kitchen["id"])["aliases"]


def test_parallel_place_files_merge_by_id():
    seed = places.seed(SID)["places"]
    k = next(p for p in seed if p["name"] == "Kitchen")
    c = next(p for p in seed if p["name"] == "Conference Room")
    put("places/f1.json", {"places": [{"id": k["id"], "aliases": ["break room"]}]})
    put("places/f2.json", {"places": [{"id": c["id"], "aliases": ["meeting room"]}]})
    assert places.validate(SID)["ok"]
    r = places.resolve(SID, "the meeting room")
    assert r["best"]["id"] == c["id"]
    assert places.resolve(SID, "break room")["best"]["id"] == k["id"]


def test_a_failing_tool_does_not_kill_the_run():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from nudge.agents.dossier import create_dossier_agent

    class Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    msgs = iter([AIMessage(content="", tool_calls=[{"id": "c1", "name": "list_panos", "args": {"sid": "no-such-scan"}}]),
                 AIMessage(content="That scan doesn't exist; carrying on.")])
    agent = create_dossier_agent(SID, model=Fake(messages=msgs))
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]},
                       config={"configurable": {"thread_id": "err"}, "recursion_limit": 20})
    err = next(m for m in out["messages"] if getattr(m, "name", None) == "list_panos")
    assert err.content.startswith("Error: list_panos") and out["messages"][-1].content.startswith("That scan")
