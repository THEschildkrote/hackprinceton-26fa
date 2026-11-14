"""nudge over imessage, through photon (beta platform, api.photon.codes).

a blind visitor already lives in imessage with voiceover, so that's the interface:
  "take me to suite 400 at 1255 S Atlantic Blvd"  -> briefing from the dossier + first instruction
  "the elevator is out"                            -> exception agent proposes a reroute
  "yes" / "no"                                     -> approve or reject it (deep agents hitl)

run: python -m nudge.photon_bot
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

from .agents.tools import dossier_dir, graph, key_places
from .instruct import meters
from .mp import ROOT, secret
from .navigator import Navigator
from .router import PROFILES

HELP = ("I'm Nudge. Text me where you're going, like: take me to the kitchen at synthetic, "
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
            return [f"{here} {meters(st['remaining'])} to go to the {s.goal}. Next: {nxt}".strip()]
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
        from photon_api.generated.models import SendMessageRequest, SendTextContent
        from photon_api.rpc_generated import SendMessageHeader, SendMessageInput, SendMessagePath
        self.photon.projects.send_message(SendMessageInput(
            path=SendMessagePath(id=self.project),
            headers=SendMessageHeader.model_validate({"idempotency-key": f"nudge:{msg['id']}:{n}"}),
            body=SendMessageRequest(spaceId=msg["space"], senderId=msg["agent"], parentMessageId=msg["id"],
                                    content=SendTextContent(type="text", format="plain", text=text))))


def serve(brain=None, poll_s=2.0):
    from photon_api import ApiError
    ch = PhotonChannel()
    brain = brain or default_brain()
    print(f"nudge on imessage for project {ch.project[:12]}...; waiting for messages")
    while True:
        try:
            for msg in ch.poll():
                print(f"<- {msg['from']}: {msg['text']}")
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
    serve()
