# Demo script: Maya's visit (3 minutes)

**The story.** Maya is blind. She has an appointment at **DJF Insurance**, a real current tenant in CoStar's record for
**1255 S Atlantic Blvd, Los Angeles**: a 1917 Victorian, now a Class B office, with no accessibility renovation on
record. She has never been there. Her brother **Sam** follows along on iMessage.

**Setup (before judges arrive).**
- Start the server: `python -m nudge.photon_bot` (serves the API on :8765 and runs the iMessage caretaker bot), or
  `python -m uvicorn nudge.server:app --port 8765` without iMessage.
- Open `http://localhost:8765/` on the projector. Laptop volume up. Leave "Ask the live AI agent" unticked unless you
  want the live-model moment (see 3b).
- Optional: on Sam's real phone, text `follow <code>` (the code is under Sam's panel) to the Nudge iMessage line, so the
  same messages also arrive on a real phone.

## 0:00 The problem (20 s)
Say: "A cane finds what's on the floor. It doesn't know which of 30 doors is the insurance office, that the front has
five steps, or that a doorway is too narrow. GPS stops at the door. Maya has to ask strangers, or not go."

## 0:20 Before she leaves (35 s) — click **Start Maya's visit**
Judges hear Maya ask what to expect, then Nudge's briefing, built from three data layers:
- CoStar: "a 2-floor office building from 1917... no accessibility renovation on record, so expect steps and narrow
  doorways."
- Vexcel + Matterport: "The entrance is at the front on Atlantic Boulevard, up about 5 steps, with no ramp."
- Routing: "about 24 meters to the door, with no street to cross... DJF Insurance is about 20 meters from the door, down
  one flight of stairs... the narrowest doorway is about 0.5 meters."

Point at **Behind the scenes**: the CoStar facts, the Vexcel oblique photo of the front steps ("the scan's elevation
data couldn't tell; the photo could"), 27 doorways found in the Matterport point cloud.

## 0:55 On the sidewalk (25 s)
"Take me to DJF Insurance." Nudge: "Route ready: 24 meters, 1 turn. Walk 19 meters, then turn right... Turn right now."
The band preview pulses left or right; the same packets go to the haptic band. Sam's phone: **"Maya started walking to
the DJF Insurance: about 24 meters."**

## 1:20 The door (15 s)
"You're at the entrance. There are about 5 steps up, then the door, 0.5 meters wide. Going inside." Say: "That came from
a facade check on Vexcel's oblique aerial imagery, not from a guess."

## 1:35 Inside (30 s)
Maya asks "How far to DJF Insurance?" — "About 20 meters to go." Then turn-by-turn in meters, a warning before every
doorway ("Narrow doorway ahead"), "Take the stairs down to Floor 1. Handrail recommended." Sam: **"Maya is now on Floor 1."**

## 2:05 Something is in the way (25 s)
"This door is locked." Nudge answers **immediately**: "Okay, stay where you are. I'm finding another way. I've found a
way around that doorway; say yes to take it." Maya: "Yes." — "New route, 15 meters." No dead air: the traveller is never
left waiting on a model.

**3b (optional live agent).** Tick "Ask the live AI agent" before starting. The LangChain exception agent reasons about
the problem and proposes a reroute that Maya approves by voice. If the free model takes more than 20 seconds, Nudge
falls back to the deterministic way around and says so. The UI shows "Thinking… N s" meanwhile.

## 2:30 Arrived (10 s)
"You have arrived." Sam: **"Maya arrived at the DJF Insurance."** That's it: three calm texts for a whole visit.

## 2:40 Help (15 s) — click **Maya: "I need help"**
Nudge: "I've let your caretaker know you need help. Stay where you are." Sam's phone, marked urgent: **"Maya asked for
help in the hallway on Floor 1: "I need help""**.

## 2:55 Close (5 s)
"The AI works ahead of the walk: it reads the building once, so every visitor after Maya gets this for free. During the
walk, guidance is deterministic and instant."

## Fallbacks
- **Wifi or the free model is down:** the scripted visit only uses deterministic paths (briefing, routing, the locked-door
  reroute), and the data is cached locally. Leave "Ask the live AI agent" unticked.
- **No sound:** every line is also shown in the Maya panel; the Sam panel works without sound.
- **Server restart:** reload `/`. "Start Maya's visit" resets the demo each time.
- **Judges want to try it:** type into "Say something as Maya" ("where is the bathroom", "where am I", "brief me"), or
  open the 3D tour link and walk it yourself.
