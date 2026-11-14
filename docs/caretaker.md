# Caretakers over iMessage

The traveller talks to Isaac by voice. The person who worries about them, a parent, partner or support worker, follows along by text through Photon iMessage. They get a few calm messages per trip and can ask for status at any time. Status questions are answered from the hub's live state, with no model call.

## Setup

1. **Traveller registers** (voice app, mobile app, or `POST /api/traveller/register {"name": "Sam"}`). This returns `{traveller_id, code}`, and the 6-digit code is shown or read to the traveller.
2. **The traveller's app passes `traveller` when it starts a trip:** `POST /api/nav/start {..., "traveller": "<traveller_id>"}`. Without it, the session id is used as the traveller id.
3. **Caretaker texts the Photon line** `follow 123456`. The caretaker must text first. A shared iMessage line cannot send an unsolicited first message, so their first text opens the conversation that all later updates go into.
4. **Run the bot and API in one process** so they share the hub: `python -m nudge.photon_bot` (API on :8765). Use `--no-api` only if nothing else needs to publish to that process.

## What gets pushed (default: important)

| When | Message |
|---|---|
| trip starts | "Sam started walking to the Conference Room: about 27 meters, roughly 1 min." |
| floor change | "Sam is now on Floor 2." |
| arrival | "Sam arrived at the Conference Room." |
| reported problem, then an approved reroute | "Sam reported: the elevator is out. They approved a new route." |
| off the map (immediate, once) | "Heads up: your traveller walked outside the mapped area near the lobby on Floor 1. Isaac is guiding them back." |
| asks for help (immediate) | "Sam asked for help in the conference room on Floor 2: "I dropped my cane"" |
| no progress for 3 minutes (immediate, once) | "Sam hasn't moved for a few minutes in the lobby on Floor 1. You may want to check in." |
| drifts off route 3 times in 2 minutes | "Sam has drifted off route a few times in the lobby. Isaac keeps rerouting." |

Rules:
- Turn-by-turn steps are never sent.
- Non-urgent messages are at least 20 s apart, with at most 8 per trip.
- The same text is never repeated within 2 minutes.
- `notify all` adds room entries and hazard warnings; `notify important` goes back to the default.

## Caretaker questions

- `where are they?`, `status`: "Sam is walking to the Conference Room, now in the lobby on Floor 1, about 18 meters to go (about 1 min). Last update just now."
- `eta`: "About 1 min to the Conference Room (18 meters left)."
- `is everything ok?`: "Yes. …" or "Not quite. …"
- `last update`: the most recent event.
- `pronouns she|he|they`: the default is they. Messages use the traveller's name and never assume a gender.
- `travel mode` / `caretaker mode`: switch this handle to the old traveller commands for testing ("take me to the kitchen at synthetic").

## Endpoints

- `POST /api/traveller/register {name?, traveller_id?}` returns `{traveller_id, code}`.
- `GET /api/traveller/{id}/status` returns `{name, state, place, floor, goal, remaining_m, eta_min, last_update_ts, recent}`.
- `POST /api/traveller/{id}/help {text}` publishes a `help` event; caretakers hear about it right away.
- All `/api/nav/*` and `/api/outdoor/nav/*` responses publish their events to the hub for the session's traveller.
