---
name: place-catalog
description: Schema, naming style, serves vocabulary and description rules for the place catalog (/dossier/places/<floor_id>.json files), the catalog that answers "how do I get to X" and "where can I get Y" at runtime. Load before writing or editing places.json.
---

# Place catalog

The router answers a visitor's question by matching their words against this catalog: exact name, then alias, then what a place *serves*, then similar words. When two places match about equally, it asks a question; it never guesses. Your job is to make the right place match and the wrong ones not match.

## Schema (`/dossier/places/<floor_id>.json`, one file per floor, merged by id into the catalog)

```json
{"places": [
  {"id": "room:3m54yff1z7crxaywd8if9rb0d", "name": "Kitchen", "kind": "room",
   "aliases": ["kitchenette", "break room"], "serves": ["water", "food", "sink"],
   "description": "Tile floor, counter along the right wall, sink under the window, a fridge hum.",
   "source": "agent", "confidence": 0.9},
  {"id": "feature:water-fountain-lobby", "name": "water fountain", "kind": "feature",
   "aliases": ["drinking fountain"], "serves": ["water"], "rooms": ["<room id from the seed>"],
   "anchor": {"pano_id": "<pano you looked from>", "bearing_deg": 135},
   "description": "Wall-mounted, waist height, next to the restroom doors.", "source": "agent", "confidence": 0.8},
  {"id": "tenant:acme-dental", "name": "Acme Dental", "kind": "tenant", "aliases": ["dentist", "Suite 400"],
   "rooms": ["<room id>"], "source": "costar", "confidence": 0.7}
], "meta": {"floor": "<floor id>", "looks": 12}}
```

- **kind:** `room | zone | feature | tenant | entrance | outdoor_zone | floor`.
- **ids:**
  - Rooms and floors reuse the seed id (`room:<id>`, `floor:<id>`) so your words override the seed.
  - New places use `feature:<short-name>`, `tenant:<name>` or `zone:<name>`.
- **Never write `polygon`, and don't copy pano lists for rooms.** Geometry comes from the scan. For a feature, the `anchor` is the location: the pano you looked from, plus the yaw you were facing when you saw it.
- **rooms:** room ids from `place_seed`, not names.

## Naming for blind travellers

- **Name a place by what it is,** in the words someone would say: "Kitchen", "restroom", "reception desk". Avoid "Room 3", "Void" and "Space B" as names; put a room number in `aliases` if there's a sign.
- **Disambiguate by a neighbour, not a colour or a sign:** "restroom by the elevator", not "blue door restroom".
- **Aliases cover synonyms and plurals people actually use:** kitchen, kitchenette, break room, pantry; restroom, bathroom, toilet, washroom. Don't reuse one alias across two different places unless they really are interchangeable; the validator warns you.
- **Tenants:** the business name plus what it is ("dentist") and the suite number.

## serves vocabulary

`water, restroom, exit, help_desk, food, seating, elevator, stairs, meeting, sink, work, corridor, laundry, gym, parking, drop_off, crossing, mail, printer, rest`

A need like "somewhere to sit" matches anything serving `seating`. When both a feature and the room it's in serve the need, the feature is chosen (the fountain, not the whole lobby), so add features for real needs: water, restroom, help_desk, seating.

## Descriptions (spoken, 25 words maximum)

Describe what can be perceived without sight: what's underfoot, sound, temperature or airflow, what a hand or cane meets, and the layout from the entry.

| Good | Bad |
|---|---|
| "Carpeted, quiet. A long table fills the middle; chairs along both sides." | "Bright room with a red accent wall." |
| "Tile floor, echoing. Reception desk straight ahead from the doors, about 4 meters." | "Modern lobby with nice art." |
| "Small, tiled, hand dryer noise. Sinks on the left as you enter." | "The bathroom." |

Distances are in meters, rounded. Don't say left or right unless it's relative to entering through the main door, and say so ("as you enter").

## Confidence

- 0.9: you saw it clearly, or the label is unambiguous.
- 0.6: likely, but partly hidden.
- 0.3: a guess. The resolver ranks low-confidence and unnamed places down.
