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
from .tools import LANDMARK_TOOLS, LEAD_TOOLS, SCOUT_TOOLS, SITE_TOOLS, dossier_dir

HERE = Path(__file__).resolve().parent
MEMORIES = ROOT / "cache" / "memories"

SUBAGENTS = {
    "hazard-scout": ("Finds and confirms hazards on one floor: protruding objects a cane misses, glass, "
                     "open stair undersides, level changes. Give it one floor id and the sid. "
                     "Writes /dossier/hazards/<floor_id>.json.", SCOUT_TOOLS),
    "landmark-writer": ("Writes non-visual landmarks (underfoot, touch, sound, air) for the turn points you list. "
                        "Give it the sid, a floor id and the pano ids from audit_routes. "
                        "Writes /dossier/landmarks/<floor_id>.json.", LANDMARK_TOOLS),
    "site-surveyor": ("Surveys the approach from outside using the aerial view and the CoStar record: accessible "
                      "entrances, drop-off, parking, building facts, tenant directory. Give it the sid. "
                      "Writes /dossier/site.md.", SITE_TOOLS),
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


def kickoff(sid):
    return (f"Build the accessibility dossier for building {sid}. Follow /agent/AGENTS.md. "
            f"Start with building_overview('{sid}') and /memories/lessons.md, plan with write_todos, "
            f"and finish with a valid /dossier/annotations.json, /dossier/audit.md and /dossier/briefing.md.")


def run(sid, model=None, on_event=print):
    agent = create_dossier_agent(sid, model)
    config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 400}
    for chunk in agent.stream({"messages": [{"role": "user", "content": kickoff(sid)}]}, config=config,
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
    a = ap.parse_args()
    out = run(a.sid, llm.model(a.model) if a.model else None)
    print(f"\ndossier in {out}")
    ann = out / "annotations.json"
    if ann.exists():
        from .tools import validate
        print(json.dumps(validate(a.sid), indent=1))


if __name__ == "__main__":
    main()
