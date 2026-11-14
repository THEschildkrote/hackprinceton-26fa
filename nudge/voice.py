"""the traveller talks to nudge. one turn in, one short spoken reply out, any front end (browser, app, phone call).

deterministic intents first (trip, where am i, repeat, how far, stop, yes/no, help); anything else while walking
goes to the exception agent, or to the explorer agent when the traveller is off the map.

mount in server.py:
    from .voice import make_router as voice_router
    app.include_router(voice_router(get_navigator, register_session=_register_voice_session))
"""
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .instruct import meters
from .navigator import Navigator

HELP_TEXT = ("Tell me where you want to go, like: take me to the kitchen. "
             "You can also ask where am I, what's next, or how far. Say I need help to alert your caretaker.")

BUILDING = re.compile(r"^(?:i'?m|i am|we'?re|we are)\s+(?:at|in)\s+(?P<b>.+)$|"
                      r"^(?:the building is|building is|set (?:the )?building(?: to)?)\s+(?P<b2>.+)$")
TRIP = re.compile(r"^(?:please\s+)?(?:take me to|bring me to|guide me to|get me to|navigate to|go to|directions to|"
                  r"how (?:do|can) i (?:get|go) to|i (?:want|need|would like|'d like) to go to|i want to get to|"
                  r"where is|where's|find)\s+(?P<place>.+?)(?:\s+(?:at|in)\s+(?P<bldg>.+))?$")
YES = {"yes", "yeah", "yep", "ok", "okay", "sure", "do it", "go ahead", "approve", "yes please", "sounds good"}
NO = {"no", "nope", "don't", "dont", "cancel that", "reject", "no thanks", "keep my route", "keep the route"}
STOP = {"stop", "cancel", "end", "end trip", "stop navigation", "cancel trip", "quit"}
REPEAT = {"repeat", "again", "say again", "what's next", "whats next", "what now", "next", "what was that",
          "repeat that", "next step"}
WHERE = {"where am i", "where am i now", "what room is this", "where are we", "what is this place"}
FAR = {"how far", "how far is it", "how much further", "how far left", "how long", "how far to go", "are we there yet"}
ORDINALS = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1, "third": 2, "three": 2, "3": 2,
            "fourth": 3, "four": 3, "4": 3, "last": -1}


def _norm(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _hub():
    try:
        from . import hub
        return hub
    except ImportError:
        return None


def _publish(tid, event):
    h = _hub()
    if h and hasattr(h, "publish"):
        try:
            h.publish(tid, {**event, "channel": "voice"})
        except Exception:
            pass  # the caretaker feed must never break the traveller's guidance


def _attach(tid, session, sid, goal, profile):
    h = _hub()
    if h and hasattr(h, "attach_session"):
        try:
            h.attach_session(tid, session, sid, goal, profile)
        except Exception:
            pass


def _default_graph(sid):
    from .agents.tools import graph
    return graph(sid)


def _default_exception(nav, g, profile_text):
    from .agents.exception import create_exception_agent
    return create_exception_agent(nav, g, profile_text)


def _default_explorer(nav, g, sid):
    from .agents.explorer import create_explorer_agent
    return create_explorer_agent(nav, g, sid=sid)


class Turn(BaseModel):
    traveller: str
    text: str
    sid: str | None = None
    x: float | None = None
    y: float | None = None
    floor: str | None = None
    heading: float | None = None
    profile: str | None = None
    session: str | None = None  # adopt a trip started elsewhere (app picker, debug page)


class Position(BaseModel):
    traveller: str
    x: float
    y: float
    floor: str
    heading: float | None = None


# events the caretaker cares about; the rest stay between traveller and band
NOTIFY = {"route", "reroute", "floor", "arrive", "unmapped", "remapped", "hazard", "room", "no_route"}


class Conversation:
    """per-traveller state: where they are, the live trip, and anything waiting on a yes or no."""

    def __init__(self, tid):
        self.tid = tid
        self.sid = None
        self.profile = "blind"
        self.session = None
        self.nav = None
        self.here = None          # (x, y, floor, heading)
        self.choice = None        # {"query", "options": [place summaries]} after an ambiguous request
        self.pending = None       # {"kind": "exception"|"explorer", "action": {...}}
        self.agents = {}          # kind -> (agent, thread)
        self.last_say = HELP_TEXT
        self.trip = None         # {goal, sid, start} of the live trip


def make_router(get_navigator=None, register_session=None, graph_for=None, exception_factory=None,
                explorer_factory=None, default_sid=None):
    """get_navigator(session) and register_session(nav, sid) -> session tie voice trips to the server's sessions,
    so the band relay and the caretaker feed follow the same navigator. factories are injectable for tests."""
    router = APIRouter(prefix="/api/voice")
    convos = {}
    graph_for = graph_for or _default_graph
    make_exception = exception_factory or _default_exception
    make_explorer = explorer_factory or _default_explorer
    own_sessions = {}

    def convo(tid):
        if tid not in convos:
            convos[tid] = Conversation(tid)
        return convos[tid]

    def live_nav(c):
        if c.session and get_navigator:
            nav = get_navigator(c.session)
            if nav is not None:
                c.nav = nav
        return c.nav

    def register(nav, sid):
        if register_session:
            return register_session(nav, sid)
        s = uuid.uuid4().hex[:10]
        own_sessions[s] = nav
        return s

    def device(nav):
        if not nav or not nav.route:
            return None
        from .device import encode_live, live_packet
        try:
            pk = live_packet(nav)
            return {"packet": pk, "line": encode_live(pk)}
        except Exception:
            return None

    def reply(c, lines, **extra):
        say = " ".join(x.strip() for x in lines if x and x.strip()) or "Okay."
        c.last_say = say
        nav = live_nav(c)
        out = {"say": say, "session": c.session, "pending": c.pending and c.pending.get("action"),
               "choice": c.choice and [o["name"] for o in c.choice["options"]]}
        if nav is not None:
            st = nav.state()
            out["state"] = {k: st.get(k) for k in ("remaining", "next", "room", "unmapped", "done", "reroutes",
                                                   "distance_to_next_m", "bearing_to_next_deg", "path")}
            out["device"] = device(nav)
            out["trip"] = c.trip
        out.update(extra)
        return out

    def where_text(c):
        nav = live_nav(c)
        g = nav.g if nav else (graph_for(c.sid) if c.sid else None)
        pos = c.here or (nav.pos + (None,) if nav and getattr(nav, "pos", None) else None)
        if g is None or pos is None:
            return "I don't know where you are yet. Tell me which building you're in."
        from .places import where_am_i
        return where_am_i(g, pos[0], pos[1], pos[2])["text"]

    def start_position(c, g):
        if c.here:
            return c.here
        from .agents.tools import key_places
        entry, _ = key_places(g)
        c.here = (entry.x, entry.y, entry.floor, None)
        return c.here

    def resolve_building(text):
        """a street address, building name or city words -> sid, via the same search the app uses."""
        from . import buildings
        t = text.strip().strip(".").lower()
        if t in buildings.HIDDEN:
            return t
        toks = re.findall(r"[a-z0-9]+", t)
        if not toks or (len(toks) < 2 and not any(ch.isdigit() for ch in t)):
            return None  # "the kitchen" is not a building
        hits = buildings.search(t, 1)
        return hits[0]["sid"] if hits else None

    def start_trip(c, place_query, bldg=None):
        if bldg:
            sid = resolve_building(bldg)
            if not sid:
                return [f"I couldn't find {bldg}. Try the street address."]
            if sid != c.sid:
                c.sid, c.here = sid, None
        if not c.sid:
            return ["Which building are you in? Say the street address."]
        try:
            g = graph_for(c.sid)
        except Exception as ex:
            return [f"I couldn't load this building's map. {ex}"]
        x, y, floor, heading = start_position(c, g)
        from .places import goal_nodes, resolve
        r = resolve(g, place_query, here=(x, y, floor), profile=c.profile)
        if r.get("ambiguous"):
            c.choice = {"query": place_query, "options": r.get("options") or []}
            return [r["question"]]
        if not r.get("best"):
            return [r.get("message") or f"I don't know where {place_query} is."]
        return begin(c, g, r["best"], goal_nodes(r["best"]), x, y, floor, heading)

    def begin(c, g, place, nodes, x, y, floor, heading):
        nav = Navigator(g, set(nodes), c.profile)
        ev = nav.start(x, y, floor, heading)
        if ev and ev[0].get("type") == "no_route":
            return [ev[0]["text"]]
        c.nav, c.choice, c.pending, c.agents = nav, None, None, {}
        c.session = register(nav, c.sid)
        name = place.get("name") if isinstance(place, dict) else str(place)
        c.trip = {"goal": name, "sid": c.sid, "start": [x, y, floor]}
        _attach(c.tid, c.session, c.sid, name, c.profile)
        _publish(c.tid, {"type": "trip_start", "goal": name, "sid": c.sid, "session": c.session,
                         "length_m": round(nav.route.length, 1), "text": f"Started a trip to the {name}."})
        lines = [f"Okay, going to the {name}."]
        lines += [e["text"] for e in ev if e.get("text") and e.get("type") in ("route", "orient", "leg")]
        return lines

    def pick_choice(c, t):
        opts = c.choice["options"]
        chosen = None
        # words that name an option beat ordinals ("floor 2" means the floor 2 one, not the second option)
        words = set(t.split()) - {"the", "one", "by", "near", "on", "please", "take", "me", "to", "go"}
        scored = [(len(words & set(_norm(o["name"]).split())), o) for o in opts]
        scored.sort(key=lambda s: -s[0])
        if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            chosen = scored[0][1]
        if chosen is None:
            idx = next((ORDINALS[w] for w in t.split() if w in ORDINALS), None)
            if idx is not None and opts and -len(opts) <= idx < len(opts):
                chosen = opts[idx]
        if chosen is None:
            return None
        g = graph_for(c.sid)
        x, y, floor, heading = start_position(c, g)
        from .places import catalog_for_graph, goal_nodes
        full = next((p for p in catalog_for_graph(g)["places"] if p["id"] == chosen.get("id")), None)
        nodes = goal_nodes(full) if full else []
        if not nodes:
            return start_trip(c, chosen["name"])
        return begin(c, g, full, nodes, x, y, floor, heading)

    def agent_turn(c, text):
        nav = live_nav(c)
        kind = "explorer" if nav.unmapped else "exception"
        if kind not in c.agents:
            try:
                agent = make_explorer(nav, nav.g, c.sid) if kind == "explorer" else \
                    make_exception(nav, nav.g, f"travels as: {c.profile}")
            except Exception as ex:
                return [f"I can't think that through right now. {ex}. Say repeat to hear your current step."]
            mod = _agent_module(kind)
            c.agents[kind] = (agent, mod.new_thread())
        agent, thread = c.agents[kind]
        _publish(c.tid, {"type": "voice_problem", "text": f"Traveller said: {text}"})
        try:
            r = _agent_module(kind).handle_event(agent, thread, text)
        except Exception as ex:
            return [f"Sorry, I couldn't work that out ({type(ex).__name__}). Stay where you are, or say I need help."]
        c.pending = {"kind": kind, "action": r["pending"]} if r.get("pending") else None
        lines = list(r.get("say") or [])
        if c.pending:
            lines.append("Say yes to take that route, or no to keep the current one.")
        return lines or ["Okay."]

    def decide(c, yes):
        kind = c.pending["kind"]
        agent, thread = c.agents[kind]
        try:
            r = _agent_module(kind).approve(agent, thread, "approve" if yes else "reject")
        except Exception as ex:
            c.pending = None
            return [f"Sorry, that didn't go through ({type(ex).__name__}). Your route is unchanged."]
        c.pending = {"kind": kind, "action": r["pending"]} if r.get("pending") else None
        lines = list(r.get("say") or [])
        nav = live_nav(c)
        if yes:
            _publish(c.tid, {"type": "reroute_approved", "text": "Traveller approved a new route."})
            nxt = nav.state().get("next") if nav else None
            if nxt and nxt.get("text"):
                lines.append(nxt["text"])
        return lines or (["New route set."] if yes else ["Okay, keeping your route."])

    @router.post("/turn")
    def turn(req: Turn):
        c = convo(req.traveller)
        if req.sid and req.sid != c.sid:
            c.sid, c.here = req.sid, None
        c.sid = c.sid or default_sid
        if req.profile:
            c.profile = req.profile
        if req.session and req.session != c.session and get_navigator and get_navigator(req.session) is not None:
            c.session, c.nav, c.pending, c.choice, c.agents = req.session, get_navigator(req.session), None, None, {}
        if req.x is not None and req.y is not None and req.floor:
            c.here = (req.x, req.y, req.floor, req.heading)
            nav = live_nav(c)
            if nav is not None and not nav.done:
                nav.update(req.x, req.y, req.floor, req.heading)
        t = _norm(req.text)
        nav = live_nav(c)

        if not t:
            return reply(c, ["I didn't catch that."])
        if re.search(r"\b(call|phone|ring|text)\b.*\b(caretaker|carer|care giver|caregiver|mom|mum|dad|family|son|"
                     r"daughter|helper)\b", t):
            _publish(c.tid, {"type": "call_request", "text": "Traveller asked you to call them.",
                             "where": where_text(c)})
            return reply(c, ["I've asked your caretaker to call you."])
        if re.search(r"\b(help|emergency|sos|i'm lost|im lost|i am lost|scared|hurt)\b", t) and t not in REPEAT:
            where = where_text(c)
            _publish(c.tid, {"type": "help", "text": f"Traveller needs help. {where}", "where": where,
                             "session": c.session})
            return reply(c, ["I've let your caretaker know you need help.", "Stay where you are.", where])
        if c.pending and (t in YES or t in NO):
            return reply(c, decide(c, t in YES))
        if c.choice:
            picked = pick_choice(c, t)
            if picked is not None:
                return reply(c, picked)
        if t in ("help me", "what can i say", "what can you do", "commands"):
            return reply(c, [HELP_TEXT])
        if t in STOP:
            if nav is None:
                return reply(c, ["You're not on a trip."])
            _publish(c.tid, {"type": "trip_end", "text": "Traveller ended the trip."})
            c.nav, c.session, c.pending, c.choice, c.agents, c.trip = None, None, None, None, {}, None
            return reply(c, ["Trip ended. I'm here when you need me."])
        if t in REPEAT:
            if nav is None:
                return reply(c, [c.last_say])
            nxt = nav.state().get("next")
            return reply(c, [nxt["text"] if nxt else "You have arrived."])
        if t in WHERE:
            return reply(c, [where_text(c)])
        if t in FAR:
            if nav is None:
                return reply(c, ["You're not on a trip yet."])
            st = nav.state()
            if st.get("done"):
                return reply(c, ["You're there."])
            lines = [f"About {meters(st['remaining'])} to go."]
            if st.get("distance_to_next_m") is not None and st.get("next"):
                lines.append(f"Next step in {meters(st['distance_to_next_m'])}.")
            return reply(c, lines)
        b = BUILDING.match(t)
        if b:
            sid = resolve_building(b["b"] or b["b2"])
            if sid:
                from .buildings import label
                if sid != c.sid:
                    c.sid, c.here = sid, None
                return reply(c, [f"Okay, {label(sid)}. Where would you like to go?"])
        m = TRIP.match(t)
        if m:
            return reply(c, start_trip(c, m["place"], m["bldg"]))
        if nav is not None and not nav.done:
            return reply(c, agent_turn(c, req.text))
        # no building yet: maybe they just said the address
        if not c.sid:
            sid = resolve_building(t)
            if sid:
                from .buildings import label
                c.sid, c.here = sid, None
                return reply(c, [f"Okay, {label(sid)}. Where would you like to go?"])
            return reply(c, ["Which building are you in? Say the street address."])
        # no trip yet: maybe they just named a place
        if c.sid:
            lines = start_trip(c, t)
            if c.nav is not None or c.choice:
                return reply(c, lines)
        return reply(c, ["I'm not sure what you mean.", HELP_TEXT])

    @router.post("/position")
    def position(req: Position):
        """front ends that track position (simulated walk, app sensors) report it here; we speak what's due."""
        c = convo(req.traveller)
        c.here = (req.x, req.y, req.floor, req.heading)
        nav = live_nav(c)
        if nav is None:
            return reply(c, [], say="", events=[])
        ev = nav.update(req.x, req.y, req.floor, req.heading)
        for e in ev:
            if e.get("type") in NOTIFY:
                _publish(c.tid, {"type": e["type"], "text": e.get("text", ""), "session": c.session,
                                 "room": e.get("room")})
        out = reply(c, [e.get("text", "") for e in ev])
        if not ev:
            out["say"] = ""
        out["events"] = [{"type": e.get("type"), "text": e.get("text"), "cue": e.get("cue")} for e in ev]
        return out

    @router.get("/state/{traveller}")
    def state(traveller: str):
        c = convo(traveller)
        return reply(c, [c.last_say])

    @router.get("/elevenlabs/tools")
    def elevenlabs_tools(request: Request, base: str | None = None):
        root = (base or str(request.base_url)).rstrip("/")
        return {"tools": elevenlabs_tool_defs(root), "notes": ELEVENLABS_NOTES}

    router.conversations = convos  # handy for tests and the caretaker feed
    return router


def _agent_module(kind):
    if kind == "explorer":
        from .agents import explorer as m
    else:
        from .agents import exception as m
    return m


ELEVENLABS_NOTES = (
    "Webhook ('server') tool for an ElevenLabs agent. Confirmed from ElevenLabs docs: type 'webhook', name, "
    "description, api_schema.url and api_schema.method. Assumed (check the current API reference when importing): "
    "the request body is described under api_schema.request_body_schema, and {{system__caller_id}} is the caller's "
    "number, used here as the traveller id. Base URL must be public (a tunnel to this server).")


def elevenlabs_tool_defs(root):
    """one tool: send what the caller said, speak back the 'say' field."""
    return [{
        "type": "webhook",
        "name": "nudge_turn",
        "description": ("Send everything the blind caller says about where they want to go, where they are, "
                        "problems on the way (elevator out, door locked, lost), yes/no answers and requests for help "
                        "to the Nudge wayfinding service. Read the 'say' field of the response to the caller "
                        "word for word. Never invent directions yourself."),
        "api_schema": {
            "url": f"{root}/api/voice/turn",
            "method": "POST",
            "request_body_schema": {
                "type": "object",
                "properties": {
                    "traveller": {"type": "string", "description": "Caller id. Use {{system__caller_id}}.",
                                  "dynamic_variable": "system__caller_id"},
                    "text": {"type": "string", "description": "What the caller just said, verbatim."},
                    "sid": {"type": "string", "description": "Building id if the caller named one; else omit."},
                },
                "required": ["traveller", "text"],
            },
        },
    }]
