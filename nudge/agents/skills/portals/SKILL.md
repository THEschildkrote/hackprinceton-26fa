---
name: portals
description: How to join a building scanned as several Matterport models (lobby, floors, suites) through portals - elevators, stairs and doors - without aligning coordinates. Use when building_models shows more than one scan, or when confirming portal_candidates.
---

# Portals between scans

## Why portals, not alignment

Every Matterport scan has its own coordinate frame: its own origin, its own rotation, its own idea of "up a floor". Aligning two scans metrically would need shared features and is easy to get subtly wrong, and a route that is 50 cm off in a corridor walks someone into a wall. So the router never compares coordinates across scans. Inside one scan, every metre and degree the haptic band gets comes from that scan's own geometry. Between scans, a portal is a transition maneuver, like a floor change: "Take the elevator up to the 35th floor."

## What makes a good portal

| Kind | Confirm when | Reject when |
|---|---|---|
| elevator | elevator doors or a call panel visible at both ends; the floors differ (scan names like "Lobby" and "35th Floor") | one end is a closet or a dumbwaiter; both scans are the same floor |
| stairs | a stair run at both ends; the same stair (handrail side, landing shape, flight direction) | open-riser decorative stair with no route to it; a stair that only connects mezzanines inside one scan |
| door / entrance | the same doorway from both sides: door type (glass, push-bar, revolving), hardware, what's visible through it | a suite door that only looks like the building entrance |

Prefer elevators: they are the step-free link and the wheelchair profile can't use stairs. Add a confirmed stair as well when you see one, because the blind profile may prefer it when the elevator is out.

## Direction and cost

- `direction` comes from floor numbers in the scan names (`35th Floor` -> 35, `Lobby` -> 0). Keep it unless your looks prove otherwise. It is stored a -> b; the router flips it for the trip back.
- `cost_m` is a walking-equivalent penalty for the transition: elevator 8 (waiting), stairs 6 per flight, door 2. Don't tune it per building.

## The note

The note is read aloud when the traveller reaches the portal. Write how to find it without sight: "elevator bank on the right side of the lobby, past the reception desk; you'll hear the chime". Never only a colour or a sign.

## Common mistakes

- Joining two scans of the same floor with an elevator (score is lowered for this; reject it).
- Confirming from one end only. Both ends need a look.
- Writing coordinates or a guessed pano id. Only `a`/`b` from `portal_candidates`; `validate_portals` rejects ids that don't exist.
