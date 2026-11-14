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
import queue
import threading
import time
from pathlib import Path
from uuid import uuid4

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import TodoListMiddleware, wrap_model_call, wrap_tool_call
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
                      "quick survey). Writes /dossier/places/<floor_id>.json.", PLACE_TOOLS),
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


READ_ONLY_AGENT = [
    FilesystemPermission(operations=["write"], paths=["/agent/**"], mode="deny"),
    # /agent is for AGENTS.md, prompts and skills; the python next to them is not the agent's business
    FilesystemPermission(operations=["read"], paths=["/agent/*.py", "/agent/**/*.py", "/agent/__pycache__/**",
                                                      "/agent/**/*.pyc"], mode="deny"),
]


def _fragment_name(content):
    """which floor a places write is about: meta.floor, else the most common floor among its entries."""
    try:
        data = json.loads(content or "")
    except (json.JSONDecodeError, TypeError):
        return "misc"
    meta = data.get("meta") if isinstance(data, dict) else None
    if isinstance(meta, dict) and meta.get("floor"):
        return str(meta["floor"])
    items = data if isinstance(data, list) else (data or {}).get("places") or []
    floors = [i.get("floor") for i in items if isinstance(i, dict) and i.get("floor")]
    return max(set(floors), key=floors.count) if floors else "misc"


@wrap_tool_call
def tool_errors_as_messages(request, handler):
    """a tool that raises must not kill the whole run: hand the error back so the model can fix its call."""
    from langchain_core.messages import ToolMessage
    try:
        return handler(request)
    except (Exception, SystemExit) as ex:  # noqa: BLE001  (mp.secret exits when a key is missing)
        tc = request.tool_call
        return ToolMessage(content=_error_advice(tc.get("name"), ex), tool_call_id=tc.get("id", ""),
                           name=tc.get("name"), status="error")


def _error_advice(name, ex):
    """tell the model whether retrying can help. free models otherwise retry the same failing call forever."""
    msg = f"{type(ex).__name__}: {str(ex)[:240]}"
    low = msg.lower()
    if any(k in low for k in ("is not set", "key rejected", "401", "403", "api_key", "not configured", "kit not")):
        return (f"Error: {name} is unavailable here ({msg}). This data source has no access in this environment. "
                f"Do not call {name} again in this run; work from what the other tools give you and note the gap.")
    if any(k in low for k in ("toomanyrequests", "429", "rate limit", "rate-limit", "provider returned error")):
        return (f"Error: {name} hit the model provider's rate limit ({msg}). Nothing was wrong with your arguments. "
                f"Retry it once on its own; if you were running several subagents at once, run them one at a time.")
    if any(k in low for k in ("no pano", "not a pano", "unknown floor", "not in this scan", "no such")):
        return (f"Error: {name} failed: {msg}. Use only ids from building_overview / list_panos / place_seed for "
                f"THIS sid. Fix the id and try once; if it fails again, skip it.")
    return (f"Error: {name} failed: {msg}. Check the arguments (exact sid, floor and pano ids) and try once more, "
            f"or skip this step and note it.")


def _is_image(b):
    return isinstance(b, dict) and b.get("type") in ("image", "image_url")


@wrap_model_call
def images_to_user_turn(request, handler):
    """look() returns images inside tool results. some providers (openrouter's free router) route on the user
    turn's modalities and hand tool-result images to text-only models, which then can't see them. so before each
    call: images from the newest tool results go into a trailing user message; older ones become a placeholder."""
    from langchain_core.messages import HumanMessage, ToolMessage
    msgs = list(request.messages)
    last_ai = max((i for i, m in enumerate(msgs) if type(m).__name__ == "AIMessage"), default=-1)
    fresh, out = [], []
    for i, m in enumerate(msgs):
        if isinstance(m, ToolMessage) and isinstance(m.content, list) and any(_is_image(b) for b in m.content):
            keep = [b for b in m.content if not _is_image(b)]
            n = len(m.content) - len(keep)
            if i > last_ai:
                fresh += [b for b in m.content if _is_image(b)]
                note = f"[{n} image(s) attached in the next user message]"
            else:
                note = f"[{n} image(s) seen earlier; call look again if you need them]"
            m = m.model_copy(update={"content": keep + [{"type": "text", "text": note}]})
        out.append(m)
    if not fresh:
        return handler(request)
    out.append(HumanMessage(content=[{"type": "text", "text": "Images returned by your last look() calls, in order:"}]
                            + fresh))
    return handler(request.override(messages=out))


@wrap_tool_call
def places_to_fragments(request, handler):
    """parallel place-indexers must not overwrite each other: a write to /dossier/places.json goes to
    /dossier/places/<floor>.json instead (the catalog merges fragments by id)."""
    tc = request.tool_call
    args = tc.get("args") or {}
    if tc.get("name") == "write_file" and str(args.get("file_path", "")).rstrip("/") == "/dossier/places.json":
        safe = "".join(ch for ch in _fragment_name(args.get("content")) if ch.isalnum() or ch in "-_") or "misc"
        tc = {**tc, "args": {**args, "file_path": f"/dossier/places/{safe}.json"}}
        request = request.override(tool_call=tc)
    return handler(request)


def create_dossier_agent(sid, model=None, checkpointer=None, vision_model=None):
    """model drives the lead; vision_model the subagents that look() (defaults: llm.spec / llm.spec('vision')).
    passing only model uses it everywhere, which is what the scripted tests do."""
    m = model if model is not None else llm.model()
    vm = vision_model if vision_model is not None else (m if model is not None else llm.model(role="vision"))
    subagents = [{"name": name, "description": desc, "system_prompt": _prompt(name), "tools": tools,
                  "skills": ["/agent/skills/"], "permissions": READ_ONLY_AGENT, "model": vm}
                 | {"middleware": [tool_errors_as_messages]
                    + ([images_to_user_turn] if any(getattr(t, "name", "") == "look" for t in tools) else [])
                    + ([places_to_fragments] if name == "place-indexer" else [])}
                 for name, (desc, tools) in SUBAGENTS.items()]
    from .finalize import finalize_dossier, merge_dossier
    return create_deep_agent(
        model=m,
        tools=LEAD_TOOLS + [merge_dossier, finalize_dossier],
        subagents=subagents,
        middleware=[TodoListMiddleware(), tool_errors_as_messages],
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
                f"Route panos (within 3 m of the planned route): {ids}. "
                f"In one turn, delegate place-indexer (only the rooms these panos are in, plus the goal) AND "
                f"hazard-scout per floor with exactly these pano ids, in parallel. Then merge_dossier, audit_routes, "
                f"landmark-writer for the turn points among these panos, merge_dossier, a short /dossier/briefing.md, "
                f"finalize_dossier. After that, continue with the full survey.")
    return (f"Build the accessibility dossier for building {sid}. Follow /agent/AGENTS.md. "
            f"Start with building_overview('{sid}'), building_models('{sid}') and /memories/lessons.md, plan with "
            f"write_todos. Define the places first (place-indexer per floor -> /dossier/places/<floor_id>.json, then "
            f"validate_places), and finish with a valid /dossier/annotations.json, /dossier/audit.md and "
            f"/dossier/briefing.md.")


MAX_MINUTES = 30      # wall clock for one run
MAX_MODEL_CALLS = 250  # across the lead and every subagent


def run(sid, model=None, on_event=print, focus=None, goal=None, vision_model=None,
        max_minutes=MAX_MINUTES, max_model_calls=MAX_MODEL_CALLS, on_chunk=None):
    """focus: pano ids to survey first (route-scoped quick dossier for a first visit); see tools.route_focus.
    whatever happens (budget, recursion limit, a flaky model), the run ends with finalize(): valid files + gaps."""
    from .finalize import finalize
    # a run must never destroy names an earlier run (or a person) already wrote: keep a copy, restore if gone
    cat = dossier_dir(sid) / "places.json"
    snapshot = cat.read_text(encoding="utf-8") if cat.exists() else None
    if snapshot is not None:
        cat.with_suffix(".json.bak").write_text(snapshot, encoding="utf-8")
    agent = create_dossier_agent(sid, model, vision_model=vision_model)
    config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 400}
    try:  # the ops dashboard shows the lead and its subagents live
        from ..traces import config_with, publish
        config = config_with(config, "dossier lead")
        publish("agent", event="run_start", agent="dossier lead", depth=0, sid=sid, goal=goal)
    except Exception:
        pass
    t0, calls, reason = time.time(), 0, "the lead finished"
    # the stream runs in a worker so the budget holds even when one model call hangs and nothing streams
    q, stop = queue.Queue(), threading.Event()

    def pump():
        try:
            for chunk in agent.stream({"messages": [{"role": "user", "content": kickoff(sid, focus, goal)}]},
                                      config=config, stream_mode="updates", subgraphs=True):
                q.put(("chunk", chunk))
                if stop.is_set():
                    return
            q.put(("end", None))
        except Exception as ex:  # recursion limit, provider errors: keep what we have
            q.put(("error", ex))

    threading.Thread(target=pump, daemon=True, name=f"dossier-{sid}").start()
    deadline = t0 + max_minutes * 60
    while True:
        left = deadline - time.time()
        if left <= 0:
            reason = f"time budget of {max_minutes} min reached"
            break
        try:
            kind, item = q.get(timeout=min(left, 5.0))
        except queue.Empty:
            continue
        if kind == "end":
            break
        if kind == "error":
            reason = f"run stopped: {type(item).__name__}: {str(item)[:200]}"
            break
        calls += _model_calls(item)
        if on_chunk:
            on_chunk(item)
        for ev in describe(item):
            on_event(ev)
        if calls >= max_model_calls:
            reason = f"budget of {max_model_calls} model calls reached"
            break
    stop.set()  # a straggling call may still finish and write a file; finalize's merge is safe to re-run
    on_event(f"[finalize] {reason}")
    summary = finalize(sid, reason)
    if snapshot is not None and _catalog_size(cat) < _catalog_size(snapshot):
        # the lead sometimes rewrites places.json in its own shape, wiping the names; keep its version aside
        if cat.exists():
            cat.with_suffix(".lead.json").write_text(cat.read_text(encoding="utf-8"), encoding="utf-8")
        cat.write_text(snapshot, encoding="utf-8")
        on_event("[finalize] places.json lost names during the run; restored the catalog from before it "
                 "(the run's version is kept as places.lead.json; per-floor agent files still merge)")
    summary["reason"], summary["model_calls"], summary["minutes"] = reason, calls, round((time.time() - t0) / 60, 1)
    on_event(f"[finalize] annotations {summary['annotations']['hazards']} hazards, "
             f"{summary['annotations']['landmarked_panos']} landmarked panos; places ok={summary['places']['ok']}; "
             f"{len(summary['gaps'])} gaps listed in /dossier/audit.md")
    run.last_summary = summary
    try:
        from ..traces import publish
        publish("agent", event="run_end", agent="dossier lead", depth=0, sid=sid, reason=reason,
                model_calls=calls, minutes=summary["minutes"])
    except Exception:
        pass
    return dossier_dir(sid)


def _catalog_size(src):
    """named rooms and zones in a places.json (a path or its text); 0 if missing or not the catalog shape."""
    try:
        text = src.read_text(encoding="utf-8") if hasattr(src, "read_text") else src
        data = json.loads(text)
    except (OSError, ValueError, TypeError):
        return 0
    ps = data.get("places") if isinstance(data, dict) else None
    return sum(1 for p in ps or [] if isinstance(p, dict) and p.get("kind") in ("room", "zone") and p.get("name"))


def _model_calls(chunk):
    _, update = chunk if isinstance(chunk, tuple) else ((), chunk)
    n = 0
    for val in (update or {}).values():
        if isinstance(val, dict):
            n += sum(1 for m in val.get("messages", []) or [] if type(m).__name__ == "AIMessage")
    return n


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


def survey_site(sid, model=None, on_event=print):
    """only the site-surveyor step, as the lead would delegate it: the approach and the entrance, checked against
    the vexcel facade imagery. for outdoor data that arrived after the survey (princeton entered vexcel's
    geofence late). writes /dossier/site.md and, through confirm_entrance, /dossier/entrances.json."""
    from langchain_core.messages import HumanMessage
    m = model if model is not None else llm.model(role="vision")
    agent = create_deep_agent(model=m, tools=SUBAGENTS["site-surveyor"][1], system_prompt=_prompt("site-surveyor"),
                              backend=backend(sid), skills=["/agent/skills/"], permissions=READ_ONLY_AGENT,
                              middleware=[tool_errors_as_messages, images_to_user_turn], name="site-surveyor")
    config = {"configurable": {"thread_id": str(uuid4())}, "recursion_limit": 150}
    try:  # the ops dashboard shows it like any other agent run
        from ..traces import config_with, publish
        config = config_with(config, "site surveyor")
        publish("agent", event="run_start", agent="site surveyor", depth=0, sid=sid, goal="entrance check")
    except Exception:
        pass
    ask = (f"sid: {sid}. Survey the approach. Above all, check the entrance candidates against the Vexcel facade "
           f"imagery and confirm the one a blind or wheelchair visitor should use, then write /dossier/site.md.")
    result = agent.invoke({"messages": [HumanMessage(content=ask)]}, config=config)
    last = next((x for x in reversed(result["messages"]) if type(x).__name__ == "AIMessage" and x.text), None)
    for x in result["messages"]:
        for tc in getattr(x, "tool_calls", None) or []:
            on_event(f"[site-surveyor] {tc['name']}({', '.join(f'{k}={v}' for k, v in tc['args'].items() if k != 'sid')})")
    return last.text if last else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("--model", default=None)
    ap.add_argument("--goal", default=None, help="route-scoped quick survey toward this room first")
    ap.add_argument("--site", action="store_true", help="only the site survey: approach and entrance from the imagery")
    a = ap.parse_args()
    if a.site:
        print(survey_site(a.sid, llm.model(a.model) if a.model else None))
        return
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
