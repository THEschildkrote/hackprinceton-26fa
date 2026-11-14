"""the dossier deep agent: walks a building's twin ahead of the visitor and writes what the router needs.

run: python -m nudge.agents.dossier <sid> [--model anthropic:claude-opus-5-5]

filesystem the agent sees:
  /dossier/   -> cache/dossiers/<sid>/   (its output; the navigator reads annotations.json from here)
  /memories/  -> cache/memories/          (lessons that carry across buildings)
  /agent/     -> nudge/agents/            (AGENTS.md, prompts, skills; read-only)
  everything else is scratch in agent state
"""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import TodoListMiddleware
from langgraph.checkpoint.memory import MemorySaver

from ..mp import ROOT
from . import llm
from .tools import (CARTOGRAPHER_TOOLS, LANDMARK_TOOLS, LEAD_TOOLS, PLACE_TOOLS, SCOUT_TOOLS, SITE_TOOLS,
                    dossier_dir)

HERE = Path(__file__).resolve().parent
MEMORIES = ROOT / "cache" / "memories"

SUBAGENTS = {
    "place-indexer": ("Defines what every section of one floor is BEFORE anything else: names unnamed rooms, adds "
                      "aliases, what each place serves (water, restroom, exit, help desk...), a short non-visual "
                      "description, features it sees (water fountain, reception desk) anchored to a pano and "
                      "bearing, and CoStar tenants. Give it the sid and one floor id (or a list of room ids for a "
                      "quick survey). Writes /dossier/places.json.", PLACE_TOOLS),
    "hazard-scout": ("Finds and confirms hazards on one floor: protruding objects a cane misses, glass, "
                     "open stair undersides, level changes. Give it one floor id and the sid. "
                     "Writes /dossier/hazards/<floor_id>.json.", SCOUT_TOOLS),
    "landmark-writer": ("Writes non-visual landmarks (underfoot, touch, sound, air) for the turn points you list. "
                        "Give it the sid, a floor id and the pano ids from audit_routes. "
                        "Writes /dossier/landmarks/<floor_id>.json.", LANDMARK_TOOLS),
    "site-surveyor": ("Surveys the approach from outside using the aerial view and the CoStar record: accessible "
                      "entrances, drop-off, parking, building facts, tenant directory. Give it the sid. "
                      "Writes /dossier/site.md.", SITE_TOOLS),
    "building-cartographer": ("Joins a building that is several Matterport scans (lobby, floors, suites). "
                              "Confirms which elevator, stairs or entrance in one scan is the same place in another, "
                              "using portal_candidates and look() at both ends. Give it the sid. Only needed when "
                              "building_models reports more than one scan. Writes /dossier/portals.json.",
                              CARTOGRAPHER_TOOLS),
}


def _prompt(name):
    p = HERE / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else \
        f"You are the {name}. {SUBAGENTS[name][0]} Load the matching skill from /agent/skills/ before you start."


def backend(sid):
    MEMORIES.mkdir(parents=True, exist_ok=True)
    return CompositeBackend(default=StateBackend(), routes={
        "/dossier/": FilesystemBackend(root_dir=dossier_dir(sid), virtual_mode=True),
        "/memories/": FilesystemBackend(root_dir=MEMORIES, virtual_mode=True),
        "/agent/": FilesystemBackend(root_dir=HERE, virtual_mode=True),
    })


READ_ONLY_AGENT = [FilesystemPermission(operations=["write"], paths=["/agent/**"], mode="deny")]


def create_dossier_agent(sid, model=None, checkpointer=None, vision_model=None):
    """model drives the lead; vision_model the subagents that look() (defaults: llm.spec / llm.spec('vision')).
    passing only model uses it everywhere, which is what the scripted tests do."""
    m = model if model is not None else llm.model()
    vm = vision_model if vision_model is not None else (m if model is not None else llm.model(role="vision"))
    subagents = [{"name": name, "description": desc, "system_prompt": _prompt(name), "tools": tools,
                  "skills": ["/agent/skills/"], "permissions": READ_ONLY_AGENT, "model": vm}
                 for name, (desc, tools) in SUBAGENTS.items()]
    return create_deep_agent(
        model=m,
        tools=LEAD_TOOLS,
        subagents=subagents,
        middleware=[TodoListMiddleware()],
        memory=["/agent/AGENTS.md"],
        skills=["/agent/skills/"],
        permissions=READ_ONLY_AGENT,
        backend=backend(sid),
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        name="dossier-lead",
    )


def kickoff(sid, focus=None, goal=None):
    if focus:
        ids = ",".join(focus)
        return (f"QUICK SURVEY FIRST for building {sid}: a visitor is about to walk to {goal or 'their destination'} "
                f"and this building has no dossier. Follow /agent/AGENTS.md, section 'Route-scoped quick survey'. "
                f"Survey only these panos first (within 3 m of the planned route): {ids}. "
                f"First delegate place-indexer for just the rooms these panos are in plus the goal, so the goal "
                f"and the rooms on the way have names and descriptions. "
                f"Delegate hazard-scout per floor with exactly these pano ids (it passes them as panos= to "
                f"overhang_candidates and glass_suspects), merge into /dossier/annotations.json and validate_annotations "
                f"right away so the router can use it. Then landmark-writer for the turn points among them that "
                f"audit_routes lists. Write a short /dossier/briefing.md. After that, continue with the full survey.")
    return (f"Build the accessibility dossier for building {sid}. Follow /agent/AGENTS.md. "
            f"Start with building_overview('{sid}'), building_models('{sid}') and /memories/lessons.md, plan with "
            f"write_todos. Define the places first (place-indexer per floor -> /dossier/places.json, then "
            f"validate_places), and finish with a valid /dossier/annotations.json, /dossier/audit.md and "
            f"/dossier/briefing.md.")


def run(sid, model=None, on_event=print, focus=None, goal=None, vision_model=None):
    """focus: pano ids to survey first (route-scoped quick dossier for a first visit); see tools.route_focus."""
    agent = create_dossier_agent(sid, model, vision_model=vision_model)
    config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 400}
    for chunk in agent.stream({"messages": [{"role": "user", "content": kickoff(sid, focus, goal)}]}, config=config,
                              stream_mode="updates", subgraphs=True):
        for ev in describe(chunk):
            on_event(ev)
    return dossier_dir(sid)


def describe(chunk):
    """turn a stream chunk into short progress lines: plans, delegations, tool calls, files."""
    ns, update = chunk if isinstance(chunk, tuple) else ((), chunk)
    who = "lead" if not ns else ns[-1].split(":")[0]
    out = []
    for node, val in (update or {}).items():
        for msg in (val or {}).get("messages", []) if isinstance(val, dict) else []:
            for call in getattr(msg, "tool_calls", None) or []:
                name, args = call["name"], call.get("args", {})
                if name == "write_todos":
                    todos = args.get("todos", [])
                    done = sum(t.get("status") == "completed" for t in todos)
                    cur = next((t["content"] for t in todos if t.get("status") == "in_progress"), "")
                    out.append(f"[{who}] plan {done}/{len(todos)} done. now: {cur}")
                elif name == "task":
                    out.append(f"[{who}] -> {args.get('subagent_type')}: {str(args.get('description', ''))[:110]}")
                elif name in ("write_file", "edit_file"):
                    out.append(f"[{who}] writes {args.get('file_path')}")
                elif name == "look":
                    out.append(f"[{who}] looks from {str(args.get('pano_id'))[:10]} at {args.get('yaw_deg')} deg")
                else:
                    out.append(f"[{who}] {name}({', '.join(f'{k}={v}' for k, v in args.items() if k != 'sid')})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("--model", default=None)
    ap.add_argument("--goal", default=None, help="route-scoped quick survey toward this room first")
    a = ap.parse_args()
    focus = None
    if a.goal:
        from .tools import route_focus
        focus = route_focus(a.sid, a.goal)["focus"]
    out = run(a.sid, llm.model(a.model) if a.model else None, focus=focus, goal=a.goal)
    print(f"\ndossier in {out}")
    ann = out / "annotations.json"
    if ann.exists():
        from .tools import validate
        print(json.dumps(validate(a.sid), indent=1))


if __name__ == "__main__":
    main()
