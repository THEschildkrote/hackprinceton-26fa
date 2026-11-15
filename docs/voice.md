# Talking to Isaac

The traveller talks; the caretaker gets status over iMessage (see the caretaker channel). Every voice front end
uses one endpoint, so the browser, the app and a phone call share the same brain:

`POST /api/voice/turn {traveller, text, sid?, session?, x?, y?, floor?, heading?, profile?}`
returns `{say, session, pending, choice, trip, state, device}`. Speak `say`. Distances are always meters.

`POST /api/voice/position {traveller, x, y, floor, heading?}` returns what's due to be spoken at that spot
(turns, doors, rooms, arrival) and forwards trip events to the caretaker feed.

## What the traveller can say

Handled without any AI model, instantly:

| Say | Isaac does |
|---|---|
| "take me to the kitchen", "how do I get to the restroom", "where is the water" | resolves the place in the building's catalog, plans the route, starts guidance |
| (when two places match) "floor 2", "the first one" | picks that one |
| "where am I" | names the room and describes it without visual cues |
| "what's next", "repeat" | the current step |
| "how far" | meters left and meters to the next step |
| "stop", "cancel" | ends the trip |
| "I need help", "I'm lost" | alerts the caretaker with the location, tells the traveller to stay put |
| "call my caretaker" | asks the caretaker to call |
| "yes" / "no" | approves or rejects a proposed reroute |

Anything else during a trip ("the elevator is out", "this door is locked") goes to the exception deep agent,
which proposes a reroute and waits for "yes". When the traveller is off the map it goes to the explorer agent.

## 1. Browser (works now)

Run the server and open `http://localhost:8765/voice.html`. Hold the big button (or hold the space bar) and speak;
release to send. Chrome on desktop and Android supports speech input; elsewhere use the text box. "Simulate walking"
walks the route so the instructions advance. The page shows the caretaker pairing code.

## 2. The app

The Where to and Navigate screens have a Talk button.

- **Expo Go:** no speech recognition module, so the Talk button is replaced by a text field and quick phrases
  (Where am I, Repeat, How far, I need help). Replies are spoken, through VoiceOver or TalkBack when they're on.
- **Development build** (`npx expo run:ios|android` or `eas build --profile development`): `expo-speech-recognition`
  is included, so Talk listens. Tap Talk, speak, tap again to send.
- **Web build:** `expo-speech-recognition` uses the browser's Web Speech API.

Settings shows the caretaker code. The app registers the traveller on first launch.

## 3. A phone call

The walker calls a number and talks to Isaac: no screen, no typing. Photon answers the call and passes it over SIP
to an ElevenLabs agent. The agent has one webhook tool. It posts what the caller said, and the caller's number
(`{{system__caller_id}}`), to `POST /api/voice/call {caller, text}`, then reads back `say`.

The caller's number is the walker. The first call links it to the Isaac phone that is online (exactly one), and
the link is kept. A trip they ask for ("I wanna go to the seminar room") then goes to that phone, the way a
caretaker's does. The phone is on their back, starts the walk from where they stand, and drives the belt.
Anything else ("how far", "where am I", "the elevator is out") is a normal voice turn on that phone's live walk.
A caller with no Isaac phone gets the voice guide on its own, at JRR unless `NUDGE_SITE` says otherwise.

Setup, with the server running and a tunnel to it (`tools/phone_line.py` keeps ids in `cache/phone_line.json`):

1. `python -m nudge.setkey ELEVENLABS_API_KEY` (an ElevenLabs key with Agents access).
2. A call-capable Photon number: shared iMessage lines can't take calls. Buy a US number in the Photon dashboard,
   or with `python tools/phone_line.py buy --area 609` (billed by Photon).
3. `python tools/phone_line.py agent --base https://<tunnel>` creates the agent and its tool.
4. `python tools/phone_line.py photon --number +1...` points Photon's default voice profile at
   `sips:sip.rtc.elevenlabs.io:5061`.
5. `python tools/phone_line.py import --number +1...` imports the number into ElevenLabs as a SIP trunk for the agent.
6. After a tunnel restart (quick tunnel URLs change): `python tools/phone_line.py base --base https://<new tunnel>`.
