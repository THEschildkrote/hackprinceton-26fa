"""nudge over imessage, through photon (beta platform, api.photon.codes), for the CARETAKER.

the traveller talks to nudge by voice; the person who worries about them follows along by text:
  "follow 123456"        -> pair with a traveller (the 6-digit code from their app)
  "where are they?"      -> place, floor, metres to go, eta, from the hub, no model call
  "notify all"           -> also room-by-room updates (default: important only)
pushed without asking: trip started, floor changes, arrived, off the map, help, stuck, approved reroutes.

the WALKER only talks: "hey siri, text isaac, i want to go to the seminar room" (siri reads the reply out).
their number is linked on its own when exactly one isaac phone is online; otherwise once, by voice too:
  "i'm 123456"                    -> this handle is the traveller with that code
  "i want to go to the kitchen"   -> a trip on their phone, as if the caretaker app sent it (default building:
                                     their last one, else NUDGE_SITE, else jrr); "... at <building>" picks one
a caretaker can send one the same way: "take them to the kitchen".

"travel mode" switches this handle to the old traveller commands (testing): "take me to X at Y".

run: python -m nudge.photon_bot            (also serves the api on :8765, so the hub is shared)
     python -m nudge.photon_bot --no-api   (bot only; caretaker pushes then need the api in this process)
env (.env): PHOTON_API_KEY, PHOTON_PROJECT_ID, optional PHOTON_ALLOWED_SENDERS (comma list of handles)
"""
import csv
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import hub
from .agents.tools import dossier_dir, graph, key_places
from .instruct import meters
from .mp import ROOT, secret
from .navigator import Navigator
from .router import PROFILES


_GENERIC = ("kitchen", "bathroom", "restroom", "lobby", "office", "hallway", "room", "conference", "elevator", "stair",
            "entrance", "lounge", "atrium", "corridor", "gallery", "laundry", "bedroom", "closet", "living", "foyer",
            "arcade", "destination", "fountain", "desk", "seminar")


def _the(name):
    """"the kitchen", but "Hallway 2, Floor 1" and "DJF Insurance" as they are."""
    if not name:
        return "the destination"
    head = name.split(",")[0]
    if re.search(r"\d", head) or not any(w in head.lower() for w in _GENERIC):
        return name
    return name if name.lower().startswith("the ") else f"the {name}"

HELP = ("I'm Isaac. Text me where you're going, like: take me to the kitchen at synthetic, "
        "or ask: where can I get water? "
        "Say wheelchair or blind to set how you travel. While walking, tell me what's wrong, "
        "like the elevator is out, and I'll find another way. Say next to repeat the step.")
GO = re.compile(r"^(?:take me|go|navigate|get me|guide me|directions)\s+to\s+(?P<place>.+?)\s+(?:at|in)\s+(?P<bldg>.+)$", re.I)
# questions about places: answered from the place catalog, no model call
ASK = re.compile(r"^(?:how (?:do|can|would|should) i (?:get|go|walk) to|how to get to|where(?:'s| is| are| can i| do i)"
                 r"|i need|i want to go to|is there|find)\b", re.I)
AT = re.compile(r"^(?P<q>.+?)\s+(?:at|in)\s+(?P<bldg>.+)$", re.I)
YES = {"yes", "y", "ok", "okay", "sure", "do it", "approve", "yep", "yeah"}
NO = {"no", "n", "nope", "reject", "don't", "dont", "cancel"}


def find_building(q):
    q = q.strip().lower().rstrip(".!?")
    if q in ("synthetic", "demo", "the demo building"):
        return "synthetic", "the demo building"
    with open(ROOT / "reference" / "models_index.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for key in ("address", "name"):
        hits = [r for r in rows if r[key] and q in r[key].lower()]
        if hits:
            # the richest scan of that building
            r = max(hits, key=lambda r: int(r["pano_count"] or 0))
            return r["sid"], r["address"] or r["name"]
    return None, None


@dataclass
class Session:
    sid: str
    name: str
    goal: str
    profile: str
    nav: Navigator
    agent: object = None
    thread: str = ""
    pending: dict | None = None


@dataclass
class Brain:
    """channel-agnostic: text in, replies out. exception_factory(nav, g, profile_text) builds the hitl agent."""
    exception_factory: object = None
    dossier_runner: object = None
    sessions: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)

    def handle(self, user, text):
        t = text.strip()
        low = t.lower().rstrip(".!?")
        s = self.sessions.get(user)
        if low in ("help", "hi", "hello", "start", "?"):
            return [HELP]
        if low in PROFILES or low in ("i use a wheelchair", "i'm blind", "im blind", "i am blind"):
            p = "wheelchair" if "wheelchair" in low else "blind" if "blind" in low else low
            self.profiles[user] = p
            return [f"Got it, I'll plan {p} routes."]
        m = GO.match(t)
        if m:
            return self._start(user, m["place"], m["bldg"])
        if ASK.match(t) and low not in ("where am i", "where am i now"):
            m2 = AT.match(t.rstrip("?"))
            if m2 and find_building(m2["bldg"])[0]:
                return self._start(user, m2["q"], m2["bldg"])
            if s:
                return self._goto(s, t)
            return ["Which building? For example: how do I get to the kitchen at synthetic."]
        if not s:
            return [HELP]
        if s.pending and low in YES | NO:
            return self._decide(s, "approve" if low in YES else "reject")
        if low in ("next", "repeat", "again", "what now"):
            nxt = s.nav.state().get("next")
            return [nxt["text"] if nxt else "You have arrived."]
        if low in ("where am i", "where am i now", "status"):
            from .places import where_am_i
            st = s.nav.state()
            here = where_am_i(s.nav.g, *s.nav.pos)["text"] if s.nav.pos else ""
            nxt = st["next"]["text"] if st["next"] else "you are there"
            return [f"{here} {meters(st['remaining'])} to go to {_the(s.goal)}. Next: {nxt}".strip()]
        if low in ("stop", "end", "quit", "cancel"):
            self.sessions.pop(user, None)
            return ["Navigation ended."]
        return self._exception(s, t)

    def _start(self, user, place, bldg):
        from . import places
        query = place.strip()
        place = re.sub(r"^(the|a|an|my)\s+", "", query, flags=re.I)
        sid, name = find_building(bldg)
        if not sid:
            return [f"I couldn't find {bldg}. Try the street address."]
        profile = self.profiles.get(user, "blind")
        try:
            g = graph(sid)
        except Exception as ex:  # no keys, model not visible, etc.
            return [f"I found {name} but couldn't load its 3D scan: {ex}"]
        entry, _ = key_places(g)
        r = places.resolve(g, query, here=(entry.x, entry.y, entry.floor), profile=profile)
        if r["ambiguous"]:
            return [r["question"]]
        if not r["best"]:
            return [r["message"].replace("I don't know a place called", f"I don't see", 1).replace(
                ". Places I know:", f" in {name}. Places I know there:", 1)]
        place = r["best"]["name"]
        nav = Navigator(g, place, profile)
        ev = nav.start(entry.x, entry.y, entry.floor)
        out = []
        brief = dossier_dir(sid) / "briefing.md"
        if brief.exists():
            out.append(brief.read_text(encoding="utf-8").strip())
        elif self.dossier_runner:
            threading.Thread(target=self.dossier_runner, args=(sid,), daemon=True).start()
            out.append("I haven't surveyed this building before. I'm doing that now; hazard warnings will improve "
                       "for your next trip.")
        s = Session(sid, name, place, profile, nav)
        self.sessions[user] = s
        out.append(f"Starting at the {entry.room_label or 'entrance'} of {name}.")
        out += [e["text"] for e in ev if e.get("text")]
        return out

    def _goto(self, s, text):
        """a place question mid-trip: resolve it from the catalog and re-route from where the traveller is."""
        from . import places
        here = s.nav.pos
        r = places.resolve(s.nav.g, text, here=here, profile=s.profile)
        if r["ambiguous"]:
            return [r["question"]]
        if not r["best"]:
            return [r["message"]]
        best = r["best"]
        nav = Navigator(s.nav.g, best["name"], s.profile)
        ev = nav.start(*here) if here else []
        s.nav, s.goal, s.agent, s.pending = nav, best["name"], None, None
        intro = f"The {best['name']}" + (f": {best['description']}" if best.get("description") else ".")
        return [intro] + [e["text"] for e in ev if e.get("text")]

    def _ensure_agent(self, s):
        if s.agent is None and self.exception_factory:
            from .agents.exception import new_thread
            s.agent = self.exception_factory(s.nav, s.nav.g, f"travels as: {s.profile}")
            s.thread = new_thread()
        return s.agent

    def _exception(self, s, text):
        if not self._ensure_agent(s):
            return ["I can't reason about that right now. Say next to hear the current step."]
        from .agents.exception import handle_event
        r = handle_event(s.agent, s.thread, text)
        s.pending = r.get("pending")
        out = list(r.get("say") or [])
        if s.pending:
            out.append("Reply yes to take this route, or no to keep the current one.")
        return out or ["Okay."]

    def _decide(self, s, decision):
        from .agents.exception import approve
        r = approve(s.agent, s.thread, decision)
        s.pending = r.get("pending")
        out = list(r.get("say") or [])
        if decision == "approve":
            nxt = s.nav.state().get("next")
            if nxt:
                out.append(nxt["text"])
        return out or (["New route set."] if decision == "approve" else ["Keeping the current route."])


class PhotonChannel:
    """polls received imessages and replies in the same space. webhooks work too; polling needs no public url."""

    def __init__(self, platform="imessage"):
        from photon_api import Photon
        self.project = secret("PHOTON_PROJECT_ID")
        self.photon = Photon(headers={"Authorization": f"Bearer {secret('PHOTON_API_KEY')}"})
        self.platform = platform
        allowed = secret("PHOTON_ALLOWED_SENDERS", required=False) or ""
        self.allowed = {a.strip() for a in allowed.split(",") if a.strip()}
        self.since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.seen = set()

    def poll(self):
        from photon_api.generated.models import TextContent, UserSnapshot
        from photon_api.rpc_generated import ListMessagesInput
        query = {"createdSince": self.since, "direction": "received", "order": "asc", "pageSize": 100,
                 "platform": self.platform}
        while True:
            page = self.photon.projects.list_messages(ListMessagesInput.model_validate({"path": {"id": self.project}, "query": query}))
            for item in page.items:
                m = item.root.root
                if m.id in self.seen or m.status != "received" or not isinstance(m.receivedBy, UserSnapshot):
                    continue
                self.seen.add(m.id)
                sender, agent = m.sentBy.root, m.receivedBy.root
                if sender.kind != "human" or agent.kind != "agent":
                    continue
                if self.allowed and sender.handle not in self.allowed:
                    continue
                if isinstance(m.content, TextContent) and m.content.text.strip():
                    yield {"id": m.id, "space": m.space.root.id, "agent": agent.id, "from": sender.handle,
                           "text": m.content.text}
            token = page.nextPageToken
            if not isinstance(token, str):
                return
            query["pageToken"] = token

    def typing(self, msg, on=True):
        from photon_api.rpc_generated import SetSpaceTypingInput
        try:
            self.photon.projects.set_space_typing(SetSpaceTypingInput.model_validate(
                {"path": {"id": self.project, "spaceId": msg["space"]}, "body": {"senderId": msg["agent"], "typing": on}}))
        except Exception:
            pass  # typing is cosmetic

    def reply(self, msg, text, n=0):
        self._send(msg["space"], msg["agent"], text, f"nudge:{msg['id']}:{n}", parent=msg["id"])

    def push(self, space_id, agent_id, text, key):
        """a message nobody asked for, into a space the caretaker opened by texting first."""
        self._send(space_id, agent_id, text, f"nudge-push:{key}")

    def _send(self, space, agent, text, key, parent=None):
        from photon_api.generated.models import SendMessageRequest, SendTextContent
        from photon_api.rpc_generated import SendMessageHeader, SendMessageInput, SendMessagePath
        body = {"spaceId": space, "senderId": agent, "content": SendTextContent(type="text", format="plain", text=text)}
        if parent:
            body["parentMessageId"] = parent
        self.photon.projects.send_message(SendMessageInput(
            path=SendMessagePath(id=self.project),
            headers=SendMessageHeader.model_validate({"idempotency-key": key[:200]}),
            body=SendMessageRequest(**body)))


# ---------- caretaker side ----------

PRONOUNS = {"they": ("they", "them", "their", "are"), "she": ("she", "her", "her", "is"),
            "he": ("he", "him", "his", "is")}
FOLLOW = re.compile(r"^(?:follow|pair|link|connect)\s*(?:with|to)?\s*([\d\s-]{6,})$", re.I)
WHERE = re.compile(r"^(?:where(?:'s| is| are)\b|status\b|eta\b|how far|how long|is (?:everything|all|she|he|they)\b"
                   r"|are (?:they|you)\b|last update|how(?:'s| is| are) (?:she|he|they|it going)|any news|update)", re.I)
URGENT = {"help", "unmapped", "stuck"}
CARETAKER_HELP = ("I'm Isaac. I keep you posted while someone you care for walks with Isaac's voice guide. "
                  "Text follow plus the 6-digit code from their app to start. Then ask: where are they? eta? "
                  "is everything ok? Text notify all for room-by-room updates, notify important to go back. "
                  "You can set pronouns with: pronouns she, he or they. "
                  "Walking with Isaac yourself? Just tell Siri: text Isaac, I want to go to the seminar room.")

# the walker, by text
ME = re.compile(r"^(?:i'?m|i am|this is|it'?s|me|walker|my code(?: is)?|code)\s*:?\s*([\d\s-]{6,})$", re.I)
WANT = re.compile(
    r"^(?:(?:hey|hi|ok|okay)\s+)?(?:isaac[,.]?\s+)?(?:can you\s+|could you\s+|please\s+)?"
    r"(?:i\s*(?:wanna|want to|need to|would like to|'?d like to|have to|gotta)\s+(?:go|get|walk|head)(?:\s+to)?"
    r"|i'?m (?:going|heading|walking) to|(?P<who>(?:take|bring|guide|get|walk|lead)\s+(?:me|them|him|her))\s+to"
    r"|navigate(?: me)? to|go to|directions to|how (?:do|can|would|should) i (?:get|go|walk) to|how to get to"
    r"|where(?:'s| is) the way to)\s+(?P<place>.+)$", re.I)
_NOT_A_PLACE = {"here", "there", "this place", "that place", "somewhere", "it"}
JRR = "R1FX6EmdcDt"


def _clean_place(text):
    t = re.sub(r"[\s.!?]+$", "", text.strip())
    t = re.sub(r"[\s,]+(?:please|pls|now|thanks|thank you)$", "", t, flags=re.I)
    return t.strip()


def _phone_online(tid):
    """True/False when this process serves the api (it sees the phone poll for trips), None when it can't tell."""
    srv = sys.modules.get("nudge.server")
    if srv is None:
        return None
    v = srv._seen.get(("phone", tid))
    return bool(v) and time.time() - v["ts"] <= srv.ONLINE_S


def _only_phone_online():
    """the one traveller whose isaac app is polling right now, or None (none, several, or no api here)."""
    srv = sys.modules.get("nudge.server")
    if srv is None:
        return None
    now = time.time()
    # registered travellers only: an isaac app registers; a phone call's own voice turns don't make it an isaac phone
    live = {tid for (role, tid), v in list(srv._seen.items()) if role == "phone" and now - v["ts"] <= srv.ONLINE_S
            and hub.has_app(tid)}
    return live.pop() if len(live) == 1 else None


def resolve_trip(text, sid_default, profile="blind"):
    """(sid, building name or None, place name), or (None, None, a question to send back)."""
    from . import places
    query = _clean_place(text)
    if not query or query.lower() in _NOT_A_PLACE:
        return None, None, "Where to? Text a place, like: I want to go to the seminar room."
    tries = []
    m = AT.match(query)
    if m:
        sid, name = find_building(m["bldg"])
        if sid:
            tries.append((sid, name, m["q"]))
    tries.append((sid_default, None, query))
    last = None
    for sid, name, q in tries:
        try:
            g = graph(sid)
        except Exception as ex:
            last = f"I couldn't load that building's 3D scan: {ex}"
            continue
        entry, _ = key_places(g)
        r = places.resolve(g, q, here=(entry.x, entry.y, entry.floor), profile=profile)
        if r["best"] and not r["ambiguous"]:
            return sid, name, r["best"]["name"]
        last = r["question"] if r["ambiguous"] else r["message"]
    return None, None, last


def _ago(ts, now_ts):
    if not ts:
        return "no updates yet"
    s = max(0, now_ts - ts)
    if s < 45:
        return "just now"
    m = round(s / 60)
    return f"{m} minute{'s' * (m != 1)} ago" if m < 90 else f"about {round(m / 60)} hours ago"


class Voice:
    """names and pronouns for one caretaker's messages."""

    def __init__(self, name, pronoun="they"):
        self.name = name
        self.p = PRONOUNS.get(pronoun or "they", PRONOUNS["they"])

    @property
    def Who(self):
        return self.name or "Your traveller"

    @property
    def who(self):
        return self.name or "your traveller"

    @property
    def them(self):
        return self.p[1]

    @property
    def they(self):
        return self.p[0]

    @property
    def is_(self):
        return self.p[3]


def _where(st):
    bits = []
    if st.get("place"):
        bits.append(f"in {_the(st['place'])}")
    if st.get("floor"):
        bits.append(f"on {st['floor']}")
    return " ".join(bits)


def status_text(st, v, now_ts=None):
    """plain-language status for a caretaker; metres, never feet."""
    now_ts = now_ts or hub.now()
    if st is None:
        return "I don't have anything for that traveller yet."
    where = _where(st)
    ago = _ago(st.get("last_update_ts"), now_ts)
    goal = st.get("goal")
    state = st["state"]
    if state == "walking" or state in ("off_route", "rerouting"):
        rem = st.get("remaining_m")
        left = f", about {meters(rem)} to go" + (f" (about {st['eta_min']} min)" if st.get("eta_min") else "") if rem is not None else ""
        extra = " Isaac just rerouted them." if state != "walking" else ""
        return f"{v.Who} is walking to {_the(goal)}" + (f", now {where}" if where else "") + f"{left}. Last update {ago}.{extra}"
    if state == "arrived":
        if (st.get("place") or "").lower() == (goal or "").lower():
            where = f"on {st['floor']}" if st.get("floor") else ""
        return f"{v.Who} arrived at {_the(goal)}" + (f", {where}" if where else "") + f". Last update {ago}."
    if state == "unmapped":
        return f"{v.Who} is outside the mapped area" + (f", last known {where}" if where else "") + \
            f". Isaac is guiding {v.them} back. Last update {ago}."
    if state == "help":
        return f"{v.Who} asked for help" + (f" {where}" if where else "") + f". Last update {ago}."
    return f"{v.Who} isn't on a trip right now" + (f"; last known {where}" if where else "") + f". Last update {ago}."


@dataclass
class CaretakerBrain:
    """caretaker texts in, replies out. travel_brain handles "travel mode" (the old traveller commands)."""
    travel_brain: object = None
    travel_mode: set = field(default_factory=set)
    pronouns: dict = field(default_factory=dict)
    waiting: dict = field(default_factory=dict)   # handle -> where they asked to go before saying who they are

    def trip(self, handle, place, for_them=False):
        """a trip on the phone of the walker who texted (or, for a caretaker, of the traveller they follow)."""
        tid = None if for_them else hub.walker(handle)
        by = "walker"
        if tid is None:
            tid, by = (hub.caretaker(handle) or {}).get("traveller"), "caretaker"
        if tid is None and not for_them:
            tid, by = _only_phone_online(), "walker"   # one isaac phone on: that's whose number this is
            if tid:
                hub.set_walker(handle, tid)
        if tid is None:
            self.waiting[handle] = place
            return ["I can't tell which Isaac phone is yours. Open Isaac on it, or say: I'm, then the 6-digit code "
                    f"from the app. Then I'll send {_the(_clean_place(place))} to it."]
        st = hub.status(tid) or {}
        profile = st.get("profile") or "blind"
        sid, bname, to = resolve_trip(place, st.get("sid") or os.environ.get("NUDGE_SITE") or JRR, profile)
        if sid is None:
            return [to]
        hub.set_trip(tid, {"sid": sid, "to": to, "profile": profile, "by": by})
        who = st.get("name")
        hub.publish(tid, {"type": "trip_requested", "goal": to,
                          "text": f"{who or 'The traveller'} asked for {_the(to)} by text." if by == "walker"
                          else f"Trip to {to} sent by text."})
        at = f" in {bname}" if bname else ""
        busy = st.get("state") in ("walking", "off_route", "rerouting")
        online = _phone_online(tid)
        if by == "caretaker":
            v = self.voice(handle)
            when = (f" It starts when {v.they} finish the current trip." if busy
                    else " It starts as soon as Isaac is open on their phone." if online is False
                    else " Their phone starts it in a few seconds.")
            return [f"Sent {_the(to)}{at} to {v.who}.{when}"]
        hi = f"Okay {who}: " if who else "Okay: "
        if busy:
            return [f"{hi}{_the(to)}{at} is next. It starts when you finish this trip or say stop."]
        if online is False:
            return [f"{hi}I sent {_the(to)}{at} to your phone. It starts as soon as Isaac is open on it."]
        return [f"{hi}your phone is starting a trip to {_the(to)}{at}. Isaac will guide you from where you are."]

    def voice(self, handle):
        c = hub.caretaker(handle) or {}
        st = hub.status(c["traveller"]) if c.get("traveller") else None
        return Voice((st or {}).get("name"), c.get("pronoun") or self.pronouns.get(handle, "they"))

    def handle(self, handle, text):
        t = text.strip()
        low = t.lower().rstrip(".!? ")
        if low == "travel mode":
            self.travel_mode.add(handle)
            return ["Travel mode on: text like a traveller, e.g. take me to the kitchen at synthetic. "
                    "Text caretaker mode to switch back."]
        if low == "caretaker mode":
            self.travel_mode.discard(handle)
            return ["Caretaker mode on."]
        if handle in self.travel_mode and self.travel_brain is not None:
            return self.travel_brain.handle(handle, t)
        if low in ("help", "hi", "hello", "start", "?", "menu"):
            return [CARETAKER_HELP]
        m = ME.match(low)
        if m:
            tid, name = hub.link_walker(handle, m.group(1))
            if not tid:
                return ["That code didn't match anyone. It's the 6-digit code in your Isaac app."]
            if handle in self.waiting:
                return self.trip(handle, self.waiting.pop(handle))
            return [f"Got it{', ' + name if name else ''}. Text me where you want to go, like: "
                    "I want to go to the seminar room."]
        m = WANT.match(t)
        if m:
            who = (m["who"] or "").lower()
            return self.trip(handle, m["place"], for_them=bool(who) and not who.endswith(" me"))
        m = FOLLOW.match(low)
        if m:
            tid = hub.pair(handle, m.group(1))
            if not tid:
                return ["That code didn't match anyone. It's the 6-digit code in the traveller's Isaac app."]
            v = self.voice(handle)
            return [f"You're now following {v.who}. I'll text you when {v.they} start a trip, change floors, "
                    f"arrive, or need help. Ask where are they? any time."]
        if low.startswith("pronoun"):
            word = low.split()[-1]
            if word in PRONOUNS:
                c = hub.caretaker(handle) or {}
                self.pronouns[handle] = word
                if c:
                    with hub._lock:
                        hub._load()["caretakers"][handle]["pronoun"] = word
                        hub._save()
                return [f"Okay, I'll say {word}."]
            return ["Say pronouns she, pronouns he, or pronouns they."]
        if low in ("notify all", "notify everything", "verbose"):
            hub.set_verbosity(handle, "all")
            return ["Okay: I'll also send room-by-room updates and hazard warnings."]
        if low in ("notify important", "quiet", "less"):
            hub.set_verbosity(handle, "important")
            return ["Okay: only starts, floor changes, arrivals and anything that needs attention."]
        c = hub.caretaker(handle) or {}
        if not c.get("traveller"):
            return ["You're not following anyone yet. Text follow plus the 6-digit code from the traveller's app."]
        st = hub.status(c["traveller"])
        v = self.voice(handle)
        if re.search(r"\b(ok|okay|alright|all right|fine|safe)\b", low):
            good = st and st["state"] in ("walking", "arrived", "idle")
            return [("Yes. " if good else "Not quite. ") + status_text(st, v)]
        if re.search(r"\beta\b|how long|how far", low):
            if st and st["state"] == "arrived":
                return [f"{v.Who} already arrived at {_the(st['goal'])}."]
            if st and st.get("remaining_m") is not None:
                return [f"About {st.get('eta_min') or 1} min to {_the(st['goal'])} ({meters(st['remaining_m'])} left)."]
            return [status_text(st, v)]
        if "last update" in low and st and st["recent"]:
            e = st["recent"][-1]
            return [f"{_ago(e.get('ts'), hub.now()).capitalize()}: {e.get('text') or e.get('type')}"]
        return [status_text(st, v)]


class Notifier:
    """turns hub events into a few calm imessages per trip. send(handle, space, agent, text, key)."""
    GAP_S = 20         # min seconds between non-urgent messages to one caretaker
    PER_TRIP = 8       # cap on non-urgent messages per trip
    DEDUPE_S = 120

    def __init__(self, send, sync=False):
        self.send, self.sync = send, sync
        self.last = {}      # handle -> ts of last non-urgent message
        self.count = {}     # (handle, session) -> non-urgent messages this trip
        self.recent = {}    # (handle, text) -> ts
        self.offroute = {}  # traveller -> [ts of off-route reroutes]
        self.flagged = {}   # traveller -> set of one-shot alerts sent this trip
        self.q = None
        if not sync:
            import queue
            self.q = queue.Queue()
            threading.Thread(target=self._worker, daemon=True).start()
        self.unsub = hub.subscribe(self.on_event)

    def _worker(self):
        while True:
            args = self.q.get()
            try:
                self.send(*args)
            except Exception as ex:
                print(f"push failed: {ex}", file=sys.stderr)

    def message(self, tid, e, verbosity, v):
        """the text for this event, or None. (urgent, text)"""
        typ, st = e.get("type"), hub.status(tid) or {}
        where = _where({**st, "place": e.get("place") or st.get("place"), "floor": e.get("floor") or st.get("floor")})
        flags = self.flagged.setdefault(tid, set())
        if typ == "route" and e.get("goal"):
            flags.clear()
            self.offroute[tid] = []
            rem = e.get("remaining_m") or st.get("remaining_m")
            eta = st.get("eta_min")
            dist = f": about {meters(rem)}" + (f", roughly {eta} min" if eta else "") if rem else ""
            return False, f"{v.Who} started walking to {_the(e['goal'])}{dist}."
        if typ == "arrive":
            return False, f"{v.Who} arrived at {_the(st.get('goal'))}."
        if typ == "help":
            quote = (e.get("text") or "").strip()
            return True, f"{v.Who} asked for help" + (f" {where}" if where else "") + (f': "{quote}"' if quote else ".")
        if typ == "stuck":
            return True, f"{v.Who} hasn't moved for a few minutes" + (f" {where}" if where else "") + \
                ". You may want to check in."
        if typ == "unmapped":
            if "unmapped" in flags:
                return None
            flags.add("unmapped")
            back = f"Isaac is guiding {v.them} back." if e.get("can_return") else f"Isaac asked {v.them} to stop and wait."
            return True, f"Heads up: {v.who} walked outside the mapped area" + (f" near {where[3:] if where.startswith('in ') else where}" if where else "") + f". {back}"
        if typ == "remapped":
            flags.discard("unmapped")
            return False, f"{v.Who} is back on the map and walking again."
        if typ == "reroute_approved":
            reason = (e.get("reason") or "").strip().rstrip(".")
            return False, (f"{v.Who} reported: {reason.lower()}. {v.they.capitalize()} approved a new route." if reason
                           else f"{v.Who} approved a new route.")
        if typ == "reroute":
            if e.get("reason"):  # a problem they reported, handled without the agent
                return False, f"{v.Who} reported: {e['reason'].strip().rstrip('.').lower()}. Isaac rerouted around it."
            if "off the route" in (e.get("text") or "").lower():
                ts = e.get("ts") or hub.now()
                hits = [t for t in self.offroute.get(tid, []) if ts - t < 120] + [ts]
                self.offroute[tid] = hits
                if len(hits) >= 3 and "offroute" not in flags:
                    flags.add("offroute")
                    return False, f"{v.Who} has drifted off route a few times" + (f" {where}" if where else "") + \
                        ". Isaac keeps rerouting."
            return None
        if e.get("floor_changed") and e.get("floor"):
            return False, f"{v.Who} is now on {e['floor']}."
        if verbosity == "all":
            if typ == "room" and e.get("text"):
                return False, f"{v.Who}: {e['text']}"
            if typ == "hazard" and e.get("text"):
                return False, f"{v.Who} passed a warning: {e['text']}"
            if typ == "reported" and e.get("text"):
                return False, f"{v.Who} reported: {e['text']}"
        return None

    def on_event(self, tid, e):
        st = hub.status(tid) or {}
        for handle in hub.caretakers(tid):
            c = hub.caretaker(handle) or {}
            if not c.get("space"):
                continue  # they haven't texted us yet, so the shared line can't reach them
            v = Voice(st.get("name"), c.get("pronoun"))
            r = self.message(tid, e, c.get("verbosity", "important"), v)
            if not r:
                continue
            urgent, text = r
            ts = e.get("ts") or hub.now()
            if ts - self.recent.get((handle, text), -1e9) < self.DEDUPE_S:
                continue
            trip = (handle, st.get("session"))
            if not urgent:
                if ts - self.last.get(handle, -1e9) < self.GAP_S and e.get("type") not in ("arrive", "route"):
                    continue
                if self.count.get(trip, 0) >= self.PER_TRIP:
                    continue
                self.last[handle] = ts
                self.count[trip] = self.count.get(trip, 0) + 1
            self.recent[(handle, text)] = ts
            args = (handle, c["space"], c.get("agent"), text, f"{tid}:{int(ts * 1000)}:{abs(hash(text)) % 10**8}")
            if self.sync:
                self.send(*args)
            else:
                self.q.put(args)


def _serve_api(port):
    import uvicorn
    from .server import app
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()
    print(f"api on http://0.0.0.0:{port} (shared hub with the caretaker bot)")


def serve(brain=None, poll_s=2.0, api_port=8765):
    from photon_api import ApiError
    ch = PhotonChannel()
    if api_port:
        _serve_api(api_port)
    brain = brain or CaretakerBrain(travel_brain=default_brain())
    Notifier(lambda handle, space, agent, text, key: ch.push(space, agent, text, key))
    print(f"nudge caretaker bot on imessage for project {ch.project[:12]}...; waiting for messages")
    while True:
        try:
            for msg in ch.poll():
                print(f"<- {msg['from']}: {msg['text']}")
                hub.set_space(msg["from"], msg["space"], msg["agent"])  # where to push updates later
                ch.typing(msg, True)
                try:
                    replies = brain.handle(msg["from"], msg["text"])
                except Exception as ex:
                    replies = [f"Sorry, something went wrong: {ex}"]
                ch.typing(msg, False)
                # one message reads better with voiceover than a burst
                text = "\n\n".join(r for r in replies if r)
                ch.reply(msg, text)
                print(f"-> {text[:160]}")
        except ApiError as ex:
            print(f"photon api error {ex.status} (request {ex.request_id or '?'})", file=sys.stderr)
            time.sleep(5)
        except Exception as ex:  # network drops etc.: imessage must never take the api down with it
            print(f"photon unreachable ({type(ex).__name__}); retrying", file=sys.stderr)
            time.sleep(5)
        time.sleep(poll_s)


def default_brain():
    def exception_factory(nav, g, profile_text):
        from .agents.exception import create_exception_agent
        return create_exception_agent(nav, g, profile_text)

    def dossier_runner(sid):
        from .agents.dossier import run
        run(sid, on_event=lambda e: print(f"   dossier {sid}: {e}"))

    from .agents import llm
    has_llm = llm.has_credentials(llm.spec()) and llm.has_credentials(llm.spec("vision"))
    return Brain(exception_factory if has_llm else None, dossier_runner if has_llm else None)


if __name__ == "__main__":
    serve(api_port=None if "--no-api" in sys.argv else int(os.environ.get("PORT") or 8765))   # PORT: a host's
