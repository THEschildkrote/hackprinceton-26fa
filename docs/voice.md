# Talking to Nudge

The traveller talks; the caretaker gets status over iMessage (see the caretaker channel). Every voice front end
uses one endpoint, so the browser, the app and a phone call share the same brain:

`POST /api/voice/turn {traveller, text, sid?, session?, x?, y?, floor?, heading?, profile?}`
returns `{say, session, pending, choice, trip, state, device}`. Speak `say`. Distances are always meters.

`POST /api/voice/position {traveller, x, y, floor, heading?}` returns what's due to be spoken at that spot
(turns, doors, rooms, arrival) and forwards trip events to the caretaker feed.

## What the traveller can say

Handled without any AI model, instantly:

| Say | Nudge does |
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

Call a number and talk to Nudge, no app needed. This needs two accounts:

- **Photon** voice on a **dedicated** number (an SMS number or a dedicated iMessage/WhatsApp number). Shared iMessage
  lines can't take calls. See https://photon.codes/docs/beta/get-started/voice.md.
- **ElevenLabs** agent, imported into ElevenLabs as a SIP-trunk number, with Photon's voice profile pointing at
  `sips:sip.rtc.elevenlabs.io:5061` over TLS.

Give the ElevenLabs agent the tool from `GET /api/voice/elevenlabs/tools?base=<public url>`. It posts what the caller
says to `/api/voice/turn`, and the agent reads back `say`. The server must be reachable from the internet (a tunnel).
The traveller id is the caller's number (`{{system__caller_id}}`).

What's confirmed from ElevenLabs docs: tool `type: "webhook"`, `name`, `description`, `api_schema.url` and `method`.
Assumed: the body schema key (`api_schema.request_body_schema`) and the caller-id dynamic variable name. Check them
against the current ElevenLabs API reference when importing.
