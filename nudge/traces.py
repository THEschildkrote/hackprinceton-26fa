"""one live feed of everything the system does, for the ops dashboard (web/ops.html).

publish(kind, **fields) from anywhere; the dashboard reads GET /api/ops/stream (sse) and /api/ops/snapshot.
kinds: session, step, position, maneuver, band, reroute, exception, agent, hub, replay.
"""
import base64
import itertools
import json
import threading
import time
from collections import deque

from langchain_core.callbacks import BaseCallbackHandler

RING = 2000
CLIP = 300

_lock = threading.Lock()
_events = deque(maxlen=RING)
_ids = itertools.count(1)
_subs = []


def clip(v, n=CLIP):
    """short, printable, never an image."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if not isinstance(v, str):
        try:
            v = json.dumps(_strip_images(v), default=str, separators=(",", ":"))
        except Exception:
            v = str(v)
    if len(v) > 200 and _looks_base64(v):
        return f"<image or blob, {len(v)} chars>"
    return v if len(v) <= n else v[:n - 1] + "…"


def _looks_base64(s):
    sample = s[:120].replace("\n", "")
    if " " in sample:
        return False
    try:
        base64.b64decode(sample + "=" * (-len(sample) % 4), validate=True)
        return True
    except Exception:
        return False


def _strip_images(o):
    if isinstance(o, dict):
        if o.get("type") == "image" or "base64" in o or "image_url" in o:
            return "<image>"
        return {k: _strip_images(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_strip_images(x) for x in o]
    return o


def publish(kind, **fields):
    ev = {"id": next(_ids), "ts": time.time(), "kind": kind,
          **{k: v for k, v in fields.items() if v is not None}}
    with _lock:
        _events.append(ev)
        subs = list(_subs)
    for fn in subs:
        try:
            fn(ev)
        except Exception:
            pass  # a slow or broken listener must never break the walk
    return ev


def subscribe(fn):
    with _lock:
        _subs.append(fn)

    def unsubscribe():
        with _lock:
            if fn in _subs:
                _subs.remove(fn)
    return unsubscribe


def recent(n=200, since=None, session=None):
    with _lock:
        evs = list(_events)
    if since is not None:
        evs = [e for e in evs if e["id"] > since]
    if session:
        evs = [e for e in evs if e.get("session") in (None, session)]
    return evs[-n:]


def reset():
    """forget everything (tests)."""
    with _lock:
        _events.clear()
        _subs.clear()


class TraceHandler(BaseCallbackHandler):
    """langchain callbacks -> 'agent' events: model calls, tool calls, subagent delegations.

    deepagents runs subagents inside the `task` tool, so a run whose ancestor is a task tool run is
    labelled with that subagent's name and indented one level.
    """

    raise_error = False

    def __init__(self, agent="agent", session=None):
        self.agent, self.session = agent, session
        self._parent, self._task, self._t0, self._model = {}, {}, {}, {}

    # --- who is this run? ----------------------------------------------------

    def _who(self, run_id):
        seen, r = 0, run_id
        while r is not None and seen < 64:
            if r in self._task:
                return self._task[r], 1
            r, seen = self._parent.get(r), seen + 1
        return self.agent, 0

    def _start(self, run_id, parent_run_id):
        self._parent[run_id] = parent_run_id
        self._t0[run_id] = time.monotonic()

    def _ms(self, run_id):
        t = self._t0.pop(run_id, None)
        return None if t is None else round((time.monotonic() - t) * 1000)

    def _emit(self, event, run_id, **f):
        who, depth = self._who(run_id)
        # the whole id: langchain run ids start with a timestamp, so a prefix collides within the minute
        publish("agent", event=event, agent=who, depth=depth, session=self.session, run=str(run_id), **f)

    # --- chains: only to know the tree --------------------------------------

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
        self._parent[run_id] = parent_run_id

    # --- models --------------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
        self._start(run_id, parent_run_id)
        inv = kw.get("invocation_params") or {}
        meta = kw.get("metadata") or {}
        model = inv.get("model") or inv.get("model_name") or meta.get("ls_model_name") or \
            (serialized or {}).get("name") or "model"
        self._model[run_id] = str(model).replace("models/", "")
        self._emit("model_start", run_id, model=self._model[run_id])

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw):
        self.on_chat_model_start(serialized, [], run_id=run_id, parent_run_id=parent_run_id, **kw)

    def on_llm_end(self, response, *, run_id, **kw):
        usage, calls, text = {}, [], ""
        try:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            if msg is not None:
                usage = getattr(msg, "usage_metadata", None) or {}
                calls = [c.get("name") for c in getattr(msg, "tool_calls", None) or []]
                text = getattr(msg, "text", "") if isinstance(getattr(msg, "text", ""), str) else ""
            else:
                text = getattr(gen, "text", "")
        except Exception:
            pass
        self._emit("model_end", run_id, model=self._model.pop(run_id, None), ms=self._ms(run_id),
                   tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"),
                   tool_calls=calls or None, text=clip(text, 160) if text else None)

    def on_llm_error(self, error, *, run_id, **kw):
        self._emit("model_error", run_id, model=self._model.pop(run_id, None), ms=self._ms(run_id),
                   error=clip(f"{type(error).__name__}: {error}", 200))

    # --- tools ---------------------------------------------------------------

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kw):
        self._start(run_id, parent_run_id)
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        args = inputs if inputs is not None else input_str
        if name == "task":
            a = args if isinstance(args, dict) else {}
            if isinstance(args, str):
                try:
                    a = json.loads(args)
                except ValueError:
                    a = {}
            sub = a.get("subagent_type") or "subagent"
            self._emit("delegate", run_id, subagent=sub, tool=name, args=clip(a.get("description"), 200))
            self._task[run_id] = sub
            return
        self._emit("tool_start", run_id, tool=name, args=clip(args))

    def on_tool_end(self, output, *, run_id, **kw):
        if run_id in self._task:
            sub = self._task[run_id]
            # report the return under the parent, not the subagent
            self._task.pop(run_id)
            self._emit("returned", run_id, subagent=sub, ms=self._ms(run_id), result=clip(_content(output), 200))
            return
        self._emit("tool_end", run_id, ms=self._ms(run_id), result=clip(_content(output)))

    def on_tool_error(self, error, *, run_id, **kw):
        self._task.pop(run_id, None)
        self._emit("tool_error", run_id, ms=self._ms(run_id), error=clip(f"{type(error).__name__}: {error}", 200))


def _content(output):
    c = getattr(output, "content", output)
    return c


def config_with(cfg, agent, session=None):
    """a langgraph config carrying a TraceHandler, without dropping callbacks already there."""
    cfg = dict(cfg or {})
    cbs = list(cfg.get("callbacks") or [])
    cbs.append(TraceHandler(agent=agent, session=session))
    cfg["callbacks"] = cbs
    return cfg
