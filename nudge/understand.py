"""what a caller meant, in their own words: the model maps free speech to one of isaac's commands and, for a place,
to one of the building's real place names. routing stays deterministic: this only decides what was asked.

    "uh, the one upstairs"           -> choose the option on the upper floor
    "where's the lounge at"          -> go to "Lounge, the ground floor"
    "hmm there's a cart in the way"  -> problem: "a cart is blocking the corridor"

returns None when there's no model, it times out, or it's unsure; callers then fall back to the plain text.
"""
import os
import re
import threading
from typing import Literal, Optional

from pydantic import BaseModel, Field

TIMEOUT_S = 6.0   # the first call of a run takes ~5 s while the model client starts; later ones ~1.5 s


class Meant(BaseModel):
    intent: Literal["go_to", "choose", "where_am_i", "how_far", "next_step", "problem", "yes", "no", "help", "stop",
                    "goodbye", "other"]
    place: Optional[str] = Field(None, description="for go_to: exactly one name from the PLACES list, copied as written")
    option: Optional[int] = Field(None, description="for choose: the 1-based number of the OPTIONS entry meant")
    problem: Optional[str] = Field(None, description="for problem: a short sentence, e.g. 'the stairs are blocked'")


PROMPT = """A blind traveller is talking to Isaac, an indoor guide, on a phone call. Speech-to-text is imperfect
(e.g. "Dino corner" may be a mishearing). Decide what they want.

Intents: go_to (a destination), choose (picking one of OPTIONS, which Isaac just offered), where_am_i, how_far,
next_step (what to do next / repeat), problem (something blocks the way: locked door, broken elevator, obstacle),
yes, no, help (they need a person), stop (end the trip), goodbye, other.

For go_to, set place to the single best match from PLACES, copied exactly; if nothing there plausibly matches, use
intent other. For choose, set option to the OPTIONS number they mean ("the upstairs one", "second", "the closer one").
Never invent a place.

PLACES:
{places}

OPTIONS (just offered; empty if none):
{options}

The traveller said: "{text}"
"""


def _place_names(sid):
    try:
        from .places import load
        return [p["name"] for p in load(sid)["places"] if p.get("name")][:120]
    except Exception:
        return []


def meant(text, sid, options=None, timeout=TIMEOUT_S):
    """a Meant, or None. never raises, never waits past `timeout`."""
    flag = os.environ.get("NUDGE_UNDERSTAND")
    if not (text or "").strip() or flag == "0" or (os.environ.get("PYTEST_CURRENT_TEST") and flag != "1"):
        return None                      # tests use the rules alone unless they ask for the model
    try:
        from .agents import llm
        if not llm.has_credentials(llm.spec()):
            return None
        names = _place_names(sid)
        msg = PROMPT.format(places="\n".join(f"- {n}" for n in names) or "(none)",
                            options="\n".join(f"{i}. {o}" for i, o in enumerate(options or [], 1)) or "(none)",
                            text=text.strip())
        runnable = llm.model(live=True).with_structured_output(Meant)
    except Exception:
        return None
    box = {}

    def run():
        try:
            box["m"] = runnable.invoke(msg)
        except Exception as ex:   # quota, network: the plain text still works
            box["err"] = ex

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    m = box.get("m")
    if not isinstance(m, Meant):
        return None
    if m.intent == "go_to" and m.place not in names:
        return None                      # it named something the building doesn't have: don't trust it
    if m.intent == "choose" and not (options and m.option and 1 <= m.option <= len(options)):
        return None
    return m


def as_command(m, options=None):
    """(text isaac's rules understand, place name or None) for what the caller meant."""
    if m is None:
        return None, None
    if m.intent == "go_to" and m.place:
        return f"take me to {m.place}", m.place
    if m.intent == "choose" and options:
        return f"take me to {options[m.option - 1]}", options[m.option - 1]
    fixed = {"where_am_i": "where am i", "how_far": "how far", "next_step": "what's next", "yes": "yes", "no": "no",
             "help": "I need help", "stop": "stop", "goodbye": "thanks, bye"}
    if m.intent in fixed:
        return fixed[m.intent], None
    if m.intent == "problem" and m.problem:
        t = m.problem.strip().rstrip(".")
        # the reroute rules listen for words like "blocked" or "in the way": make sure the problem says so
        if not re.search(r"blocked|in the way|out of service|out of order|locked|closed|broken|obstacle", t, re.I):
            t += ", it's in the way"
        return t, None
    return None, None
