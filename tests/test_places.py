"""the place catalog: the scan seeds it, the place-indexer agent names it, runtime questions resolve through it."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nudge import fetch, places
from nudge.agents import tools as T
from tests.synthetic import building

SID = "synthetic"


def with_closet():
    """the synthetic building plus a small unlabelled (Void) closet off the kitchen."""
    m = building()
    f1 = next(f for f in m["floors"] if f["id"] == "f1")
    pts = [(2.0, 12.0), (4.0, 12.0), (4.0, 13.5), (2.0, 13.5)]
    vids = []
    for i, (x, y) in enumerate(pts):
        vid = f"vc{i}"
        f1["vertices"].append({"id": vid, "position": {"x": x, "y": y}})
        vids.append(vid)
    eids = []
    for i in range(4):
        eid = f"ec{i}"
        f1["edges"].append({"id": eid, "type": "opening" if i == 0 else "wall", "thickness": 0.1,
                            "vertices": [{"id": vids[i]}, {"id": vids[(i + 1) % 4]}]})
        eids.append({"id": eid})
    m["rooms"].append({"id": "r_closet", "floor": {"id": "f1"}, "classifications": [{"label": "Void"}],
                       "boundary": {"edges": eids}})
    m["panoLocations"].append({"id": "p_X1", "label": "X1", "position": {"x": 3.0, "y": 12.8, "z": 1.5},
                               "floor": {"id": "f1"}})
    return m


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    real = fetch.load
    monkeypatch.setattr(fetch, "load", lambda sid, refresh=False: with_closet() if sid == SID else real(sid, refresh))
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    shutil.rmtree(T.dossier_dir(SID), ignore_errors=True)
    T._graph_cached.cache_clear()


def rid_of(g, label):
    return next(r for r, v in g.rooms.items() if v["label"] == label)


def agent_catalog(g):
    k = rid_of(g, "Kitchen")
    return {"places": [
        {"id": "room:r_closet", "name": "storage closet", "aliases": ["closet", "storage"], "serves": [],
         "description": "Small and carpeted, shelves on both sides, off the kitchen.", "confidence": 0.8},
        {"id": f"room:{k}", "aliases": ["kitchenette", "break room"], "serves": ["water", "food", "sink"],
         "description": "Tile floor, fridge hum, counter along the far wall."},
        {"id": "feature:water-tap", "name": "water tap", "kind": "feature", "aliases": ["sink"],
         "serves": ["water"], "rooms": [k], "anchor": {"pano_id": "p_K2", "bearing_deg": 90},
         "description": "Kitchen sink with a tap, at counter height.", "confidence": 0.8},
    ], "meta": {"floor": "f1", "looks": 2}}


def write_catalog(g):
    (T.dossier_dir(SID) / "places.json").write_text(json.dumps(agent_catalog(g)), encoding="utf-8")


def test_seed_from_the_scan():
    cat = places.seed(SID)
    names = {p["name"] for p in cat["places"]}
    for n in ("Kitchen", "Lobby", "Office", "Conference Room", "Elevator, Floor 1", "Elevator, Floor 2",
              "Stairs, Floor 1", "Stairs, Floor 2", "Floor 1", "Floor 2"):
        assert n in names, (n, names)
    kitchen = next(p for p in cat["places"] if p["name"] == "Kitchen")
    assert kitchen["polygon"] and kitchen["entry_panos"] and "water" in kitchen["serves"]
    closet = next(p for p in cat["places"] if p["id"] == "room:r_closet")
    assert closet["unnamed"] and closet["name"].startswith("unnamed space") and "kitchen" in closet["name"]
    assert any(p["kind"] == "tenant" and p["name"] == "Acme Dental" and p["rooms"] for p in cat["places"])


def test_agent_words_win_geometry_stays_from_the_scan():
    g = T.graph(SID)
    cat = agent_catalog(g)
    cat["places"][0]["polygon"] = [[0, 0], [1, 0], [1, 1]]  # an authored polygon is ignored
    (T.dossier_dir(SID) / "places.json").write_text(json.dumps(cat), encoding="utf-8")
    merged = places.load(SID)
    closet = next(p for p in merged["places"] if p["id"] == "room:r_closet")
    assert closet["name"] == "storage closet" and closet["polygon"] != [[0, 0], [1, 0], [1, 1]]
    assert places.validate(SID)["ok"] is False  # and the validator calls it out
    write_catalog(g)
    v = places.validate(SID)
    assert v["ok"] and v["features"] == 1, v


def test_resolve_names_aliases_needs_and_ambiguity():
    g = T.graph(SID)
    write_catalog(g)
    here = (2.0, 1.5, "f1")  # lobby
    r = places.resolve(g, "how do I get to the kitchenette?", here=here)
    assert r["best"]["name"] == "Kitchen" and r["why"] == "alias"
    r = places.resolve(g, "somewhere to get water", here=here)
    assert r["best"]["name"] == "water tap" and not r["ambiguous"]
    assert [c["name"] for c in r["candidates"][:2]] == ["water tap", "Kitchen"]
    assert places.goal_nodes(r["best"]) == ["p_K2"]
    r = places.resolve(g, "where is the conference room", here=here)
    assert r["best"]["name"] == "Conference Room"
    r = places.resolve(g, "the elevator", here=here)
    assert r["ambiguous"] and "Floor 1" in r["question"] and "meter" in r["question"] or \
        r["best"]["name"] == "Elevator, Floor 1"
    r = places.resolve(g, "the elevator")  # no position: always a question
    assert r["ambiguous"] and r["question"].endswith("Which one?")
    r = places.resolve(g, "swimming pool", here=here)
    assert r["best"] is None and "Kitchen" in r["message"] and r["suggestions"]


def test_router_and_where_am_i_use_the_catalog():
    from nudge.router import plan, resolve_goal
    g = T.graph(SID)
    write_catalog(g)
    assert resolve_goal(g, "break room") == set(next(
        p for p in places.load(SID)["places"] if p["name"] == "Kitchen")["panos"])
    assert plan(g, "p_L1", "kitchenette", "blind").nodes[-1] in {"p_K1", "p_K2", "p_K3"}
    w = places.where_am_i(g, 4.0, 9.0, "f1")
    assert w["place"]["name"] == "Kitchen" and "Tile floor" in w["text"] and "water tap" in w["text"]
    out = places.plan_to(g, "p_L1", "the elevator", here=None)
    assert out.get("ambiguous") and out["question"]


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def call(i, name, **args):
    return {"id": f"c{i}_{name}", "name": name, "args": args}


def ai(*calls, text=""):
    return AIMessage(content=text, tool_calls=list(calls))


def test_place_indexer_subagent_names_the_floor():
    from nudge.agents.dossier import create_dossier_agent
    g = T.graph(SID)
    catalog = agent_catalog(g)
    msgs = [
        # lead
        ai(call(1, "building_overview", sid=SID)),
        ai(call(2, "task", subagent_type="place-indexer", description="sid synthetic, floor f1")),
        # place-indexer
        ai(call(3, "read_file", file_path="/agent/skills/place-catalog/SKILL.md"), call(3, "place_seed", sid=SID, floor="f1")),
        ai(call(4, "look", sid=SID, pano_id="p_K2", yaw_deg=90)),
        ai(call(5, "write_file", file_path="/dossier/places.json", content=json.dumps(catalog))),
        ai(call(6, "validate_places", sid=SID)),
        AIMessage(content="Named 1 room, enriched the kitchen, 1 feature. Wrote /dossier/places.json."),
        # lead
        ai(call(7, "validate_places", sid=SID)),
        AIMessage(content="Places defined."),
    ]
    agent = create_dossier_agent(SID, model=ToolFake(messages=iter(msgs)))
    out = agent.invoke({"messages": [{"role": "user", "content": "define the places"}]},
                       config={"configurable": {"thread_id": "places"}, "recursion_limit": 100})
    assert out["messages"][-1].content == "Places defined."
    assert (T.dossier_dir(SID) / "places.json").exists()
    seed = next(m for m in out["messages"] if getattr(m, "name", None) == "validate_places")
    assert json.loads(seed.content)["ok"]
    assert places.resolve(g, "break room")["best"]["name"] == "Kitchen"
    assert places.resolve(g, "the storage closet")["best"]["id"] == "room:r_closet"


def test_photon_bot_answers_place_questions_without_a_model():
    from nudge.photon_bot import Brain
    g = T.graph(SID)
    write_catalog(g)
    b = Brain(None, None)  # no exception agent, no dossier runner: nothing can call a model
    b.handle("+1", "take me to the conference room at synthetic")
    out = " ".join(b.handle("+1", "how do I get to the kitchenette?"))
    assert "Kitchen" in out and "meter" in out and "feet" not in out
    assert b.sessions["+1"].goal == "Kitchen"
    out = " ".join(b.handle("+1", "where can I get water"))
    assert "water tap" in out
    out = " ".join(b.handle("+2", "how do I get to the kitchen at synthetic"))
    assert "Route ready" in out
    assert "Which building" in b.handle("+3", "where is the restroom")[0]


def test_server_places_endpoints():
    from nudge.server import app
    c = TestClient(app)
    g = T.graph(SID)
    write_catalog(g)
    cat = c.get(f"/api/places/{SID}").json()
    assert any(p["name"] == "water tap" for p in cat["places"])
    r = c.get(f"/api/places/{SID}/resolve", params={"q": "kitchenette", "x": 2, "y": 1.5, "floor": "f1"}).json()
    assert r["best"]["name"] == "Kitchen" and r["best"]["goal_nodes"]
    s = c.post("/api/nav/start", json={"sid": SID, "to": "somewhere to get water", "profile": "blind",
                                       "x": 2, "y": 1.5, "floor": "f1"})
    assert s.status_code == 200 and s.json()["place"]["name"] == "water tap"
    amb = c.post("/api/nav/start", json={"sid": SID, "to": "the elevator", "profile": "blind",
                                         "x": 2, "y": 1.5, "floor": "f1"})
    # exact room labels keep the old behaviour (nearest); only free text goes through the catalog
    assert amb.status_code in (200, 409)
    w = c.get(f"/api/places/{SID}/where", params={"x": 4, "y": 9, "floor": "f1"}).json()
    assert w["place"]["name"] == "Kitchen"
