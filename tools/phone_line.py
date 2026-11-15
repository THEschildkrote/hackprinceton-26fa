"""the phone line: the walker calls a number and talks to isaac, no screen, no typing.

photon answers the call and hands it over sip to an elevenlabs agent. the agent has one tool: it posts what the
caller said, and the caller's number, to /api/voice/call, then reads back `say`. a trip they ask for starts on the
isaac phone on their back (see server.voice_call).

  python -m nudge.setkey ELEVENLABS_API_KEY                  once
  python tools/phone_line.py buy --area 609                   buys a us number on photon (billed by photon)
  python tools/phone_line.py agent --base https://<tunnel>    makes or updates the agent and its tool
  python tools/phone_line.py photon --number +1...            photon sends that number's calls to elevenlabs
  python tools/phone_line.py import --number +1...            elevenlabs takes calls for that number (sip trunk)
  python tools/phone_line.py base --base https://<tunnel>     after a tunnel restart (quick tunnel urls change)
  python tools/phone_line.py status
  python tools/phone_line.py setup --base https://<tunnel>    all of the above in one go (buys a number if none)
  python tools/phone_line.py imessage --add +1... [--name X]  lets that phone text the isaac imessage line

ids go in cache/phone_line.json; keys stay in .env and are never printed.
"""
import argparse
import json
import re
import sys
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nudge.mp import secret  # noqa: E402
from nudge.voice import elevenlabs_tool_defs  # noqa: E402

STATE = ROOT / "cache" / "phone_line.json"
EL = "https://api.elevenlabs.io"
SIP = "sips:sip.rtc.elevenlabs.io:5061"
NAME = "Isaac"
FIRST = "Hi, this is Isaac. Where would you like to go?"
PROMPT = """You are Isaac, the voice of a wayfinding service for blind and low-vision travellers. The caller is \
walking with Isaac: a phone on their back and a belt that taps the way.

For everything the caller says (where they want to go, where they are, how far, what's next, a problem on the way \
like a locked door or a broken elevator, yes or no, a request for help) call isaac_turn with their exact words. \
Then say the "say" field of the result, word for word, and nothing else. If "say" is empty, stay quiet.

Never make up directions, distances, rooms or buildings. Never describe things by how they look. Keep your own \
words to a minimum: the caller is walking. Distances are in meters."""


def load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def el(method, path, body=None):
    r = requests.request(method, EL + path, json=body, timeout=30,
                         headers={"xi-api-key": secret("ELEVENLABS_API_KEY"), "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise SystemExit(f"elevenlabs {method} {path}: {r.status_code} {r.text[:600]}")
    return r.json() if r.content else {}


def e164(number):
    d = re.sub(r"\D", "", number or "")
    if len(d) == 10:
        d = "1" + d
    if len(d) < 11:
        raise SystemExit(f"{number!r} doesn't look like a phone number; give it as +1XXXXXXXXXX")
    return "+" + d


def masked(number):
    return number[:5] + "***" + number[-3:] if number else "?"


# ---------- elevenlabs ----------

def tool(base, st):
    cfg = elevenlabs_tool_defs(base.rstrip("/"))[0]
    if st.get("tool_id"):
        el("PATCH", f"/v1/convai/tools/{st['tool_id']}", {"tool_config": cfg})
    else:
        st["tool_id"] = el("POST", "/v1/convai/tools", {"tool_config": cfg})["id"]
    st["base"] = base.rstrip("/")
    save(st)
    print(f"tool {st['tool_id']} -> {cfg['api_schema']['url']}")


def agent(base):
    st = load()
    tool(base, st)
    conv = {"agent": {"first_message": FIRST, "language": "en",
                      "prompt": {"prompt": PROMPT, "llm": "gemini-2.5-flash", "tool_ids": [st["tool_id"]]}}}
    body = {"name": NAME, "conversation_config": conv}
    try:
        if st.get("agent_id"):
            el("PATCH", f"/v1/convai/agents/{st['agent_id']}", body)
        else:
            st["agent_id"] = el("POST", "/v1/convai/agents/create", body)["agent_id"]
    except SystemExit as ex:
        if "llm" not in str(ex).lower():
            raise
        conv["agent"]["prompt"].pop("llm")        # that model isn't offered: take elevenlabs' default
        if st.get("agent_id"):
            el("PATCH", f"/v1/convai/agents/{st['agent_id']}", body)
        else:
            st["agent_id"] = el("POST", "/v1/convai/agents/create", body)["agent_id"]
    save(st)
    print(f"agent {st['agent_id']} ({NAME}) uses the tool; try it in the elevenlabs dashboard before calling")


def import_number(number):
    st = load()
    if not st.get("agent_id"):
        raise SystemExit("make the agent first: python tools/phone_line.py agent --base https://<tunnel>")
    number = e164(number)
    r = el("POST", "/v1/convai/phone-numbers", {
        "provider": "sip_trunk", "phone_number": number, "label": f"{NAME} (photon)", "agent_id": st["agent_id"],
        "inbound_trunk_config": {"media_encryption": "allowed"}})
    st["el_number_id"], st["number"] = r.get("phone_number_id"), number
    save(st)
    print(f"elevenlabs takes calls for {masked(number)} with the {NAME} agent ({st['el_number_id']})")


# ---------- photon ----------

def photon():
    from photon_api import Photon
    return Photon(headers={"Authorization": f"Bearer {secret('PHOTON_API_KEY')}"}).projects.platforms, \
        secret("PHOTON_PROJECT_ID")


def resources():
    from photon_api import rpc_generated as R
    P, pid = photon()
    return P.list_resources(R.ListResourcesInput.model_validate({"path": {"id": pid}})).resources


def number_of(res):
    d = json.dumps(res.detail)
    m = re.search(r"\+\d{11,15}", d)
    return m.group(0) if m else None


def route_photon(number):
    from photon_api import ApiError
    from photon_api import rpc_generated as R
    P, pid = photon()
    number = e164(number)
    res = next((r for r in resources() if number_of(r) == number), None)
    if res is None:
        raise SystemExit(f"{masked(number)} isn't a number in this photon project. Buy one: "
                         "python tools/phone_line.py buy --area 609 (or in the photon dashboard)")
    try:
        prof = P.get_default_voice_profile(R.GetDefaultVoiceProfileInput.model_validate({"path": {"id": pid}}))
    except ApiError:
        prof = P.create_default_voice_profile(R.CreateDefaultVoiceProfileInput.model_validate(
            {"path": {"id": pid}, "body": {"protocol": "sip", "mediaEncryption": "auto"}}))
    P.replace_voice_profile_inbound(R.ReplaceVoiceProfileInboundInput.model_validate({
        "path": {"id": pid, "profileId": prof.profileId},
        "body": {"destinationUri": SIP, "credentials": None, "expectedVersion": prof.version}}))
    st = load()
    st.update(photon_profile=prof.profileId, photon_resource=res.resourceId, number=number)
    save(st)
    print(f"photon sends calls to {masked(number)} to {SIP} (default voice profile {prof.profileId}); "
          f"abilities: {', '.join(res.abilities)}")


def buy(area):
    from photon_api import rpc_generated as R
    P, pid = photon()
    body = {"countryCode": "US", **({"areaCode": area} if area else {})}
    op = P.purchase_sms_number(R.PurchaseSmsNumberInput.model_validate(
        {"path": {"id": pid}, "body": body, "headers": {"Idempotency-Key": f"isaac-{uuid.uuid4()}"}}))
    print(f"photon is buying a US number{' in ' + area if area else ''}: operation "
          f"{getattr(op, 'operationId', None) or op}. Run status in a minute to see it.")


def wait_for_number(timeout_s=180):
    """a bought number shows up as a resource once photon's operation finishes."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for r in resources():
            n = number_of(r)
            if n and r.state == "active":
                return n
        time.sleep(5)
    raise SystemExit("photon hasn't delivered the number yet; run setup again in a minute")


def setup(base, area="609"):
    have = [number_of(r) for r in resources() if number_of(r)]
    if not have:
        buy(area)
    number = have[0] if have else wait_for_number()
    print(f"number {masked(number)}")
    route_photon(number)
    if not secret("ELEVENLABS_API_KEY", required=False):
        raise SystemExit("photon is ready. Now: python -m nudge.setkey ELEVENLABS_API_KEY, then run setup again")
    agent(base)
    st = load()
    if st.get("el_number_id") and st.get("number") == number:
        print("elevenlabs already has the number")
    else:
        import_number(number)
    print(f"done: call {number} and say where you want to go")


def imessage_add(handle, name=None):
    """the shared imessage line only takes texts from handles added to it (25 at most); each gets its own line."""
    from photon_api import rpc_generated as R
    a = photon()[0].imessage.assignments
    pid = photon()[1]
    h = handle if "@" in handle else e164(handle)
    body = {"handle": h, **({"firstName": name} if name else {})}
    r = a.create(R.CreateSharedLineAssignmentInput.model_validate(
        {"path": {"id": pid}, "body": body, "headers": {"Idempotency-Key": f"isaac-{uuid.uuid4()}"}}))
    print(f"{masked(h) if not '@' in h else h[:3] + '***'} can now text Isaac at {r.assignedPhoneNumber}")
    return r.assignedPhoneNumber


def status():
    st = load()
    print("saved:", {k: (masked(v) if k == "number" else v) for k, v in st.items()})
    if secret("ELEVENLABS_API_KEY", required=False) and st.get("agent_id"):
        a = el("GET", f"/v1/convai/agents/{st['agent_id']}")
        print("agent:", a.get("name"), "| tools:", a.get("conversation_config", {}).get("agent", {})
              .get("prompt", {}).get("tool_ids"))
    if st.get("tool_id"):
        t = el("GET", f"/v1/convai/tools/{st['tool_id']}")
        print("tool url:", t.get("tool_config", {}).get("api_schema", {}).get("url"))
    try:
        rs = resources()
        print("photon numbers:", [(masked(number_of(r)), r.state, r.abilities) for r in rs] or "none yet")
    except Exception as ex:
        print("photon:", type(ex).__name__, str(ex)[:200])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["agent", "base", "photon", "import", "buy", "status", "setup", "imessage"])
    ap.add_argument("--base", help="public url of this server (the cloudflare tunnel)")
    ap.add_argument("--number", help="the photon number, +1XXXXXXXXXX")
    ap.add_argument("--area", help="three-digit us area code for buy", default=None)
    ap.add_argument("--add", help="imessage: the phone number (or email) to let text the line")
    ap.add_argument("--name", help="imessage: their first name")
    a = ap.parse_args()
    if a.cmd in ("agent", "base", "setup") and not a.base:
        ap.error("--base https://<tunnel> is needed")
    if a.cmd in ("photon", "import") and not a.number:
        ap.error("--number +1XXXXXXXXXX is needed")
    if a.cmd == "agent":
        agent(a.base)
    elif a.cmd == "base":
        tool(a.base, load())
    elif a.cmd == "photon":
        route_photon(a.number)
    elif a.cmd == "import":
        import_number(a.number)
    elif a.cmd == "buy":
        buy(a.area)
    elif a.cmd == "setup":
        setup(a.base, a.area or "609")
    elif a.cmd == "imessage":
        if not a.add:
            ap.error("--add +1XXXXXXXXXX is needed")
        imessage_add(a.add, a.name)
    else:
        status()


if __name__ == "__main__":
    main()
