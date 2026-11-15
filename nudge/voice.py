"""the traveller talks to nudge. one turn in, one short spoken reply out, any front end (browser, app, phone call).

deterministic intents first (trip, where am i, repeat, how far, stop, yes/no, help); anything else while walking
goes to the exception agent, or to the explorer agent when the traveller is off the map.

mount in server.py:
    from .voice import make_router as voice_router
    app.include_router(voice_router(get_navigator, register_session=_register_voice_session))
"""
import re
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .instruct import meters
from .navigator import Navigator

HELP_TEXT = ("Tell me where you want to go, like: take me to the kitchen. "
             "You can also ask where am I, what's next, or how far. Say I need help to alert your caretaker.")

BUILDING = re.compile(r"^(?:i'?m|i am|we'?re|we are)\s+(?:at|in)\s+(?P<b>.+)$|"
                      r"^(?:the building is|building is|set (?:the )?building(?: to)?)\s+(?P<b2>.+)$")
TRIP = re.compile(r"^(?:(?:hey|hi|ok|okay|uh|um|er|so|well|isaac|please)\s+)*"
                  r"(?:take me to|bring me to|guide me to|get me to|walk me to|navigate to|go to|directions to|"
                  r"how (?:do|can) i (?:get|go) to|i (?:want|need|would like|'d like) to go to|i want to get to|"
                  r"i (?:wanna|gotta|gonna) (?:go|get|head) to|i (?:want|need) to (?:get|head) to|"
                  r"where is|where's|find)\s+(?P<place>.+?)(?:\s+(?:at|in)\s+(?P<bldg>.+))?$")
# "from point 12 to point 40": a trip between two named scan points (jrr names its 66 points by number), starting at
# the first instead of the entrance
FROM_TO = re.compile(r"^(?:(?:hey|hi|ok|okay|uh|um|so|well|isaac|please)\s+)*"
                     r"(?:(?:take|walk|bring|guide|get) me |go |navigate |i (?:want|need) to go |how do i get )?"
                     r"from\s+(?P<a>.+?)\s+to\s+(?P<b>.+?)$")
YES = {"yes", "yeah", "yep", "ok", "okay", "sure", "do it", "go ahead", "approve", "yes please", "sounds good"}
NO = {"no", "nope", "don't", "dont", "cancel that", "reject", "no thanks", "keep my route", "keep the route"}
STOP = {"stop", "cancel", "end", "end trip", "stop navigation", "cancel trip", "quit"}
REPEAT = {"repeat", "again", "say again", "what's next", "whats next", "what now", "next", "what was that",
          "repeat that", "next step"}
WHERE = {"where am i", "where am i now", "what room is this", "where are we", "what is this place"}
BRIEF = {"brief me", "briefing", "what should i know", "what do i need to know", "tell me about this building",
         "tell me about the building", "what's this building like", "whats this building like", "what to expect"}
ACK = "Okay, stay where you are. I'm finding another way."
# something on the way is wrong: always a reroute question, never a place to look up
PROBLEM = re.compile(r"\b(locked|blocked|out of service|out of order|closed|broken|can'?t get through|cannot get through|"
                     r"won'?t open|doesn'?t open|no way through|elevator (?:is )?out|stairs? (?:are |is )?blocked|"
                     r"construction|in the way|obstacle)\b")
# a question about a place: answered from the place catalog; during a trip we offer to switch, never call a model
PLACEQ = re.compile(r"^(?:please\s+)?(?:where (?:can|could|do|should) i (?:get|find|buy|go for|go to)|"
                    r"where(?:'s| is| are)(?: the| a| an)?|is there (?:a|an|any|somewhere)|"
                    r"how (?:do|can|would|should) i (?:get|go) to|how to get to|find(?: me)?|i need(?: a| an| the| some)?|"
                    r"i'?m looking for)\s+(?P<place>.+?)(?:\s+(?:at|in)\s+(?P<bldg>.+?))?\s*\??$")
LOST = re.compile(r"\b(i'?m lost|i am lost|lost|where do i go|which way|i don'?t know where)\b")
FAR = {"how far", "how far is it", "how much further", "how far left", "how long", "how far to go", "are we there yet"}
# "ok, thanks, bye": nothing to do, and never a problem to reroute around
THANKS = re.compile(r"^(?:(?:ok(?:ay)?|got it|thanks?|thank you(?: so much)?|cool|great|perfect|alright|all right|bye|"
                    r"goodbye|see you|sounds good|isaac|then|so)\b[\s,]*)+$")
# answers to "is something in the way?" that mean nothing is
CLEAR = NO | {"keep going", "nothing", "nothing there", "it's fine", "its fine", "all clear", "clear", "i'm fine",
              "im fine", "i'm okay", "im okay", "carry on", "continue", "go on", "never mind", "nevermind", "no it's fine"}
STALL_ANSWER_S = 60.0
ORDINALS = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1, "third": 2, "three": 2, "3": 2,
            "fourth": 3, "four": 3, "4": 3, "last": -1}


_YES_W = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "approve", "approved"}
_NO_W = {"no", "nope", "nah", "don't", "dont", "cancel", "reject"}
_FILL_W = {"uh", "um", "er", "hmm", "oh", "so", "well", "please", "thanks", "thank", "you", "isaac", "do", "it", "go",
           "ahead", "that", "sounds", "good", "take", "the", "route", "let's", "lets", "keep", "my", "current", "one",
           "i", "said", "already", "fine", "great", "then"}


def answer(t):
    """True for a yes, False for a no, None when it's something else. short replies only, so a sentence that
    happens to contain "sure" never approves a reroute."""
    if t in YES:
        return True
    if t in NO:
        return False
    w = t.split()
    if not w or len(w) > 7 or any(x not in _YES_W | _NO_W | _FILL_W for x in w):
        return None
    yes, no = any(x in _YES_W for x in w), any(x in _NO_W for x in w)
    return None if yes == no else yes


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


def _venue_names(sid):
    """spoken name -> scan point label for this building, from nudge/venue_names.json (lowercase, no "the")."""
    import json
    from pathlib import Path
    try:
        names = json.loads((Path(__file__).with_name("venue_names.json")).read_text()).get(sid or "", {})
    except Exception:
        return {}
    return {re.sub(r"^the\s+", "", k.strip().lower()): str(v) for k, v in names.items()}


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
    async_ok: bool = False      # client can poll /job/{id}: agent turns answer at once with an acknowledgement
    agent: bool = True          # false: skip the ai agent and use the deterministic reroute (scripted demo)


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
        self.briefed = False      # offered the pre-visit briefing already
        self.last_session = None  # the most recent trip, for problems reported after it ended or was dropped
        self.stall = None         # the traveller stalled and was asked "is something in the way?"


def make_router(get_navigator=None, register_session=None, graph_for=None, exception_factory=None,
                explorer_factory=None, default_sid=None, agent_timeout=20.0, clock=time.monotonic,
                has_caretaker=None, on_turn=None, on_stall_answer=None):
    """get_navigator(session) and register_session(nav, sid) -> session tie voice trips to the server's sessions,
    so the band relay and the caretaker feed follow the same navigator. factories are injectable for tests.
    on_turn(traveller, heard, reply) sees every exchange (heard is None for a late agent answer)."""
    router = APIRouter(prefix="/api/voice")
    convos = {}
    graph_for = graph_for or _default_graph
    make_exception = exception_factory or _default_exception
    make_explorer = explorer_factory or _default_explorer
    own_sessions = {}
    jobs = {}  # id -> agent turn running in the background

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
        say = re.sub(r"\s+", " ", re.sub(r"[*_`#>]+|\[(.*?)\]\(.*?\)", lambda m: m.group(1) or "", say)).strip()
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
        if g is not None and pos is None:
            pos = start_position(c, g)   # no walk yet: they're at the entrance, where every trip starts
        if g is None or pos is None:
            return "I don't know where you are yet. Tell me which building you're in."
        from .places import where_am_i
        return where_am_i(g, pos[0], pos[1], pos[2])["text"]

    def start_position(c, g):
        # mid-walk, a new trip starts where the traveller is now, not where this walk began. with the belt
        # that is also what lets it carry on without lining up again (belt.send_route's continuation)
        nav = live_nav(c)
        if nav is not None and not nav.done and getattr(nav, "pos", None) and \
                (nav.g is g or (c.trip or {}).get("sid") == c.sid):
            x, y, floor = nav.pos[:3]
            return (x, y, floor, getattr(nav, "heading", None))
        if c.here:
            return c.here
        # the same "entrance" a trip from the app starts at (nudge.pdr.resolve_start): a scan point named
        # entrance, then the building's front door, then the best guess from the room names
        try:
            from .pdr import resolve_start
            entry = resolve_start(g, c.sid, "entrance")
        except Exception:
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

    def point_named(g, text):
        """the scan points a spoken name means: a name from venue_names.json ("the elevator"), "point 12",
        "number 12", "12", or a point's own label -> their ids."""
        q = re.sub(r"^the\s+", "", (text or "").strip().strip(".").lower())
        q = _venue_names(getattr(g, "sid", None)).get(q, q)
        q = re.sub(r"^(?:point|number|spot|scan point)\s+", "", q)
        return {n.id for n in g.nodes.values() if (n.label or "").lower() == q}

    def start_trip(c, place_query, bldg=None, origin=None):
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
        if origin is not None:
            start = point_named(g, origin)
            if not start:
                return [f"I don't know a point called {origin} here. Say a point number, like from point 3 to point 40."]
            n = g.nodes[next(iter(start))]
            c.here = (n.x, n.y, n.floor, None)   # they're standing there now, not at the entrance
            x, y, floor, heading = c.here
        else:
            x, y, floor, heading = start_position(c, g)
        from .places import goal_nodes, resolve
        # a scan point named outright ("point 40", or "the finish" on a drawn map) is the spot itself, as it is
        # for a trip the app starts: not the whole room around it, which the traveller may already be standing in
        named = point_named(g, place_query)
        if named:
            label = next(g.nodes[i].label for i in named)
            spoken = re.sub(r"^the\s+", "", place_query.strip().strip(".").lower())
            if spoken in _venue_names(getattr(g, "sid", None)):
                label = spoken   # "going to the elevator", in their own words ("the" is added when said)
            elif label.isdigit():
                label = f"point {label}"   # jrr's points are numbers: "going to point 40", not "going to 40"
            return begin(c, g, {"name": label}, named, x, y, floor, heading)
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
        from .places import the
        said = the(place)
        c.last_session = c.session
        c.trip = {"goal": name, "sid": c.sid, "start": [x, y, floor]}
        _attach(c.tid, c.session, c.sid, name, c.profile)
        _publish(c.tid, {"type": "trip_start", "goal": name, "sid": c.sid, "session": c.session,
                         "length_m": round(nav.route.length, 1), "text": f"Started a trip to {said}."})
        lines = [f"Okay, going to {said}."]
        lines += [e["text"] for e in ev if e.get("text") and e.get("type") in ("route", "orient", "leg")]
        if not c.briefed:
            c.briefed = True
            lines.append("Say brief me any time to hear what to expect in this building.")
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
        r = _agent_module(kind).handle_event(agent, thread, text)
        pending = {"kind": kind, "action": r["pending"]} if r.get("pending") else None
        lines = list(r.get("say") or [])
        if pending:
            lines.append("Say yes to take that route, or no to keep the current one.")
        return lines or ["Okay."], pending

    def fallback(c, text):
        """no agent, or it is taking too long: avoid the thing they named on the route ahead, if a way around exists."""
        nav = live_nav(c)
        if nav is None or not nav.route or not getattr(nav, "pos", None):
            return ["Stay where you are. Say I need help and I'll let your caretaker know."], None
        from .router import NoRoute, plan
        g, t = nav.g, text.lower()
        nodes, cum, prog = nav.route.nodes, nav.cum, nav.progress
        ahead = [(a, b) for i, (a, b) in enumerate(zip(nodes, nodes[1:])) if cum[i + 1] > prog + 0.1]
        cands = []
        for want in ("elevator", "stairs"):
            if want[:5] in t:
                cands += [("node", b) for a, b in ahead if want in g.nodes[b].tags]
        if not cands:
            doors = [("edge", (a, b)) for a, b in ahead if "door" in g.edge(a, b).tags]
            cands = doors + [("edge", e) for e in ahead if ("edge", e) not in doors]
        x, y, fl = nav.pos[:3]
        try:
            start = nav._start_node(x, y, fl)
        except Exception:
            start = nodes[0]
        remaining = max(0.0, nav.route.length - prog)
        what = "the elevator" if "elevator" in t else "the stairs" if "stair" in t else "that doorway"
        for kind, target in cands:
            be = set(nav.blocked_edges) | ({tuple(target)} if kind == "edge" else set())
            bn = set(nav.blocked_nodes) | ({target} if kind == "node" else set())
            if start in bn:
                continue
            try:
                r = plan(g, start, nav.goal, nav.profile, be, bn)
            except NoRoute:
                continue
            extra = max(0.0, r.length - remaining)
            args = {"block_node": target} if kind == "node" else {"block_edge": list(target)}
            pending = {"kind": "fallback", "reason": text.strip(),
                       "action": {"name": "reroute", "args": args,
                                  "description": f"Avoid {what}; new route about {meters(r.length)}."}}
            lines = [f"I've found a way around {what}; say yes to take it."]
            if extra >= 1:
                lines.append(f"It adds about {meters(extra)}.")
            return lines, pending
        return ["I can't find another way from here. Stay where you are; say I need help and I'll let your "
                "caretaker know."], None

    def run_agent(c, text, use_agent=True):
        """start the agent turn in the background; returns the job id."""
        _publish(c.tid, {"type": "voice_problem", "text": f"Traveller said: {text}"})
        jid = uuid.uuid4().hex[:10]
        job = {"tid": c.tid, "text": text, "started": clock(), "done": False, "result": None, "error": None,
               "closed": False}
        jobs[jid] = job
        if not use_agent:
            job["done"], job["error"] = True, "agent off"
            return jid

        def work():
            try:
                job["result"] = agent_turn(c, text)
            except Exception as ex:
                job["error"] = f"{type(ex).__name__}: {ex}"
            job["done"] = True
        threading.Thread(target=work, daemon=True).start()
        return jid

    def settle(jid):
        """the outcome of a job, once: the agent's answer, or the way around after a timeout or failure.
        None while still thinking."""
        job = jobs.get(jid)
        if job is None or job["closed"]:
            return None
        c = convo(job["tid"])
        late = clock() - job["started"] >= agent_timeout
        if job["done"] and job["result"] and not job["error"]:
            lines, pending = job["result"]
        elif job["done"] or late:
            lines, pending = fallback(c, job["text"])
        else:
            return None
        job["closed"] = True
        job["fallback"] = not (job["result"] and not job["error"])
        c.pending = pending
        return lines

    def offer_place(c, nav, query):
        """mid-trip place question: say where it is and offer to go, keeping the current trip until they say yes."""
        from .places import goal_nodes, resolve, the
        x, y, fl = nav.pos[:3] if getattr(nav, "pos", None) else start_position(c, nav.g)[:3]
        r = resolve(nav.g, query, here=(x, y, fl), profile=c.profile)
        if r.get("ambiguous"):
            c.choice = {"query": query, "options": r.get("options") or []}
            return [r["question"]]
        best = r.get("best")
        if not best:
            return [r.get("message") or f"I don't have {query} on record here."]
        cand = next((k for k in r.get("candidates") or [] if (k.get("place") or {}).get("id") == best.get("id")), None)
        dist = cand and cand.get("route_m")
        goal = (c.trip or {}).get("goal")
        c.pending = {"kind": "goto", "place": best,
                     "action": {"name": "go_to", "args": {"place": best.get("name")},
                                "description": f"Go to {the(best)} instead."}}
        lines = [f"{the(best)[0].upper() + the(best)[1:]} is" + (f" about {meters(dist)} away." if dist else " nearby.")]
        lines.append(f"Say yes to go there now" + (f", or no to keep going to {goal}." if goal else "."))
        return lines

    def decide(c, yes):
        kind = c.pending["kind"]
        if kind == "goto":
            p, c.pending = c.pending, None
            nav = live_nav(c)
            if not yes or nav is None:
                return [f"Okay, still going to {(c.trip or {}).get('goal') or 'your destination'}."]
            from .places import goal_nodes
            x, y, fl = nav.pos[:3] if getattr(nav, "pos", None) else start_position(c, nav.g)[:3]
            return begin(c, nav.g, p["place"], goal_nodes(p["place"]), x, y, fl, None)
        if kind == "fallback":
            p, c.pending = c.pending, None
            nav = live_nav(c)
            if not yes or nav is None:
                return ["Okay, keeping your route."]
            args = p["action"]["args"]
            x, y, fl = nav.pos[:3]
            a, b = (args["block_edge"] if "block_edge" in args else (args["block_node"], None))
            ev = nav.block(x, y, fl, a, b, why="Okay.")
            _publish(c.tid, {"type": "reroute_approved", "text": "Traveller approved a new route.",
                             "reason": p.get("reason")})
            lines = [e["text"] for e in ev if e.get("text") and e.get("type") in ("reroute", "orient", "leg", "no_route")]
            return lines or ["New route set."]
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

    def told(tid, heard, out):
        if on_turn:
            try:
                on_turn(tid, heard, out)
            except Exception:
                pass  # the dashboard must never cost the traveller an answer
        return out

    @router.post("/turn")
    def turn(req: Turn):
        return told(req.traveller, req.text, _turn(req))

    def _turn(req):
        c = convo(req.traveller)
        if req.sid and req.sid != c.sid:
            c.sid, c.here = req.sid, None
        c.sid = c.sid or default_sid
        if req.profile:
            c.profile = req.profile
        if req.session and req.session != c.session and get_navigator and get_navigator(req.session) is not None:
            c.session, c.nav, c.pending, c.choice, c.agents = req.session, get_navigator(req.session), None, None, {}
            c.last_session = req.session
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
        if re.search(r"\b(help|emergency|sos|scared|hurt)\b", t) and t not in REPEAT:
            where = where_text(c)
            # the caretaker sees the traveller's own words; where they are travels separately
            _publish(c.tid, {"type": "help", "text": req.text.strip().rstrip("."), "where": where,
                             "session": c.session})
            h = _hub()
            if has_caretaker is not None:
                paired = has_caretaker(c.tid)
            else:
                paired = bool(h and h.caretakers(c.tid)) if h and hasattr(h, "caretakers") else True
            if not paired and h and hasattr(h, "register_traveller"):
                code = h.register_traveller(c.tid)
                return reply(c, [f"No caretaker is paired yet; your code is {' '.join(code)}.",
                                 "Stay where you are.", where])
            return reply(c, ["I've let your caretaker know you need help.", "Stay where you are.", where])
        if c.pending and answer(t) is not None:
            return reply(c, decide(c, answer(t)))
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
        stall = c.stall if c.stall and clock() - c.stall["t"] < STALL_ANSWER_S else None
        asks = t in WHERE or t in FAR or t in BRIEF or LOST.search(t) or re.match(r"^(how far|how much further|how long)\b", t)
        if stall and not asks:
            # the answer to "is something in the way?"
            if answer(t) is True:
                return reply(c, ["What's in the way? Tell me, and I'll find a way around it."])
            c.stall = None
            outcome = "clear" if t in CLEAR else "obstacle"
            if on_stall_answer:
                try:
                    on_stall_answer(c.tid, stall, outcome, req.text)
                except Exception:
                    pass
            if outcome == "clear":
                nxt = nav.state().get("next") if nav is not None else None
                return reply(c, ["Okay, carry on."] + ([nxt["text"]] if nxt and nxt.get("text") else []))
            if nav is not None and not nav.done:
                return agent_reply(c, req, f'The traveller stopped {stall["where"]} and, asked whether something '
                                           f'is in the way, said: "{req.text.strip()}"')
        if PROBLEM.search(t):
            if nav is None and c.last_session and get_navigator:
                old = get_navigator(c.last_session)
                if old is not None and old.route and not old.done:
                    c.session, c.nav = c.last_session, old
                    nav = old
            if nav is None or nav.done:
                return reply(c, ["Where were you going? Tell me, and I'll find a way around it."])
        elif LOST.search(t) and t not in REPEAT and not (nav is not None and getattr(nav, "unmapped", False)):
            if nav is None:
                return reply(c, [where_text(c), "Where would you like to go?"])
            nxt = nav.state().get("next")
            return reply(c, [where_text(c), "Your next step: " + (nxt["text"] if nxt else "you're almost there.")])
        if t in WHERE:
            return reply(c, [where_text(c)])
        if t in BRIEF:
            if not c.sid:
                return reply(c, ["Which building are you in? Say the street address."])
            from .demo import briefing
            b = briefing(c.sid, (c.trip or {}).get("goal"))
            c.briefed = True
            return reply(c, b["sentences"], briefing=b["sentences"])
        if t in FAR or re.match(r"^(how far|how much further|how long)\b", t):
            if nav is None:
                return reply(c, ["You're not on a trip yet."])
            st = nav.state()
            if st.get("done"):
                return reply(c, ["You're there."])
            lines = [f"About {meters(st['remaining'])} to go."]
            if st.get("distance_to_next_m") is not None and st.get("next"):
                lines.append(f"Next step in {meters(st['distance_to_next_m'])}.")
            return reply(c, lines)
        if PROBLEM.search(t) and nav is not None and not nav.done:
            b = None
        else:
            b = BUILDING.match(t)
        if b:
            sid = resolve_building(b["b"] or b["b2"])
            if sid:
                from .buildings import label
                if sid != c.sid:
                    c.sid, c.here = sid, None
                return reply(c, [f"Okay, {label(sid)}. Where would you like to go?"])
        q = None if PROBLEM.search(t) else PLACEQ.match(t)
        if q and nav is not None and not nav.done and not q["bldg"]:
            return reply(c, offer_place(c, nav, q["place"]))
        ft = None if PROBLEM.search(t) else FROM_TO.match(t)
        if ft:
            return reply(c, start_trip(c, ft["b"], origin=ft["a"]))
        m = None if PROBLEM.search(t) else TRIP.match(t)
        if m is None and q:
            return reply(c, start_trip(c, q["place"], q["bldg"]))
        if m:
            return reply(c, start_trip(c, m["place"], m["bldg"]))
        if THANKS.match(t):
            return reply(c, ["Okay. I'm here if you need me."])
        if nav is not None and not nav.done:
            return agent_reply(c, req, req.text)
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

    def agent_reply(c, req, text):
        """hand `text` to the exception agent: an acknowledgement at once for clients that poll, else wait."""
        jid = run_agent(c, text, use_agent=req.agent)
        # "stay where you are, I'm finding another way" only when they reported a problem, not for a question
        ack = ACK if PROBLEM.search(_norm(text)) or c.stall else "One moment."
        if req.async_ok:
            lines = settle(jid) if not req.agent else None
            if lines is not None:
                return reply(c, [ack] + lines, say_now=ack, pending_job=None)
            return reply(c, [ack], say_now=ack, pending_job=jid)
        # one-shot clients: wait for the agent, but never past the timeout
        lines = settle(jid)
        while lines is None:
            time.sleep(0.2)
            lines = settle(jid)
        return reply(c, ([ack] if ack == ACK else []) + lines)

    def note_stall(tid, info):
        """the tracker saw this traveller stall and Isaac asked; their next words are the answer."""
        c = convo(tid)
        s = info.get("session")
        if s and s != c.session and get_navigator and get_navigator(s) is not None:
            c.session, c.nav, c.pending, c.choice, c.agents = s, get_navigator(s), None, None, {}
            c.last_session = s
        c.stall = {**info, "t": clock()}

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

    @router.get("/job/{jid}")
    def job(jid: str):
        """poll an agent turn: {done: false} while thinking, then the spoken result (the agent's, or the way around)."""
        j = jobs.get(jid)
        if j is None:
            raise HTTPException(404, "unknown job")
        c = convo(j["tid"])
        if j["closed"]:
            return reply(c, [c.last_say], done=True, fallback=j.get("fallback"))
        lines = settle(jid)
        if lines is None:
            return {"done": False, "say": "", "elapsed_s": round(clock() - j["started"], 1)}
        return told(j["tid"], None, reply(c, lines, done=True, fallback=j.get("fallback")))

    @router.get("/state/{traveller}")
    def state(traveller: str):
        c = convo(traveller)
        return reply(c, [c.last_say])

    @router.get("/elevenlabs/tools")
    def elevenlabs_tools(request: Request, base: str | None = None):
        root = (base or str(request.base_url)).rstrip("/")
        return {"tools": elevenlabs_tool_defs(root), "notes": ELEVENLABS_NOTES}

    router.conversations = convos  # handy for tests and the caretaker feed
    router.turn = turn            # the phone-call webhook (server.voice_call) speaks through the same brain
    router.note_stall = note_stall
    router.jobs = jobs
    return router


def _agent_module(kind):
    if kind == "explorer":
        from .agents import explorer as m
    else:
        from .agents import exception as m
    return m


ELEVENLABS_NOTES = (
    "Webhook tool for the ElevenLabs agent behind the Photon phone number (tools/phone_line.py creates it). "
    "It posts what the caller said and their number ({{system__caller_id}}) to /api/voice/call; the agent reads "
    "back 'say'. Base URL must be public (a tunnel to this server).")


def elevenlabs_tool_defs(root):
    """one tool: send what the caller said, speak back the 'say' field."""
    return [{
        "type": "webhook",
        "name": "isaac_turn",
        "description": ("Send everything the blind caller says to the Isaac wayfinding service: where they want to go, "
                        "where they are, problems on the way (elevator out, door locked, lost), yes or no answers and "
                        "requests for help. Then read the 'say' field of the response to the caller word for word. "
                        "Never invent directions yourself."),
        "response_timeout_secs": 30,
        "api_schema": {
            "url": f"{root}/api/voice/call",
            "method": "POST",
            "request_body_schema": {
                "type": "object",
                "properties": {
                    "caller": {"type": "string", "dynamic_variable": "system__caller_id"},
                    "conversation": {"type": "string", "dynamic_variable": "system__conversation_id"},
                    "text": {"type": "string", "description": "What the caller just said, word for word."},
                },
                "required": ["text"],
            },
        },
    }]
