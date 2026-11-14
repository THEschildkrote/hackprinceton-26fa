---
name: hazard-taxonomy
description: What counts as a hazard for a blind or low-vision traveller, the ADA 2010 limits behind it, what a long cane detects versus misses, and how to score confidence. Use before confirming, rejecting or reviewing hazard candidates.
---

# Hazard taxonomy

## The core idea: what a cane misses
A long cane sweeps an arc on or just above the floor, about shoulder width, one or two steps ahead. It finds anything that reaches down to the floor or close to it: walls, furniture legs, planters, steps up. It misses things whose leading edge is in the air with open space beneath: wall-mounted objects, overhangs, the underside of stairs, hanging signs. Those meet the traveller at the torso or head. That is what the 2010 ADA Standards section 307 is about.

## ADA 2010 Standards, section 307 (protruding objects)
- **307.2 Protrusion limits.** Objects with leading edges **more than 27 in (685 mm) and not more than 80 in (2030 mm)** above the finish floor may protrude **4 in (100 mm) max** horizontally into the circulation path. Handrails may protrude 4.5 in (115 mm).
- **307.3 Post-mounted objects.** Free-standing objects on posts or pylons may overhang **12 in (305 mm) max** where they are 27-80 in above the floor. A sign or object between two posts more than 12 in apart needs its lowest edge at 27 in or lower, or above 80 in.
- **307.4 Vertical clearance.** At least **80 in (2030 mm)** of headroom. Where headroom drops below 80 in (for example under a stair), a guardrail or barrier must be provided with its leading edge **27 in max** above the floor, so a cane finds it.
- **307.5 Required clear width.** Protruding objects may not reduce the required clear width of an accessible route (36 in min, 403.5.1).

A protrusion of more than 4 in in the 27-80 in band, or headroom under 80 in with no cane-detectable barrier, is a hazard. Even a compliant 4 in protrusion is worth noting when it sits right at a turn point.

## Hazard kinds (use these exact strings)
| kind | what it looks like | why it matters |
|---|---|---|
| `protruding_object` | wall sconce, wall-mounted TV or monitor, fire extinguisher cabinet that isn't recessed, shelf, drinking fountain without knee-level wing walls, phone box | leading edge in the 27-80 in band; the cane passes under it |
| `low_overhang` | beam, duct, hanging sign or light, open door closer arm, under 80 in | head strike |
| `open_stair_underside` | the space under a stair or escalator where headroom falls below 80 in and nothing at floor level blocks it | classic head strike; very common in lobbies |
| `glass_wall` | full-height glazing in a walking area | the cane finds it, but it is often missed by low-vision visitors, reflects sound oddly and breaks wall-trailing assumptions; scans often lose it |
| `glass_door` | glass door or sidelight | same, plus the door swings |
| `floor_level_change` | a single step, a raised threshold, a sunken area, a ramp start | a single unexpected step is a fall risk; mark the pano with node tag `step` too |
| `drop_off` | mezzanine edge, loading dock, stage, unguarded edge | fall risk |
| `column` | free-standing column in a walking line | the cane finds it, but worth a warning where the route passes close |
| `other` | anything else; explain in the note | |

## Related requirements, for context
- **Stairs, 504.3:** open risers are not permitted on stairs that are part of an accessible means of egress. **504.6 / 505:** handrails on both sides. Note stairs with open risers or a missing handrail in the note or as a node tag on the stair pano.
- **Changes in level, 303:** vertical change up to 1/4 in is allowed; 1/4-1/2 in must be beveled; more than 1/2 in must be ramped. A visible lip or threshold above about 1/2 in is a `floor_level_change`.
- **Doors, 404.2.3:** 32 in min clear opening. A narrower opening on a route is an edge tag `narrow`, not a hazard.
- **Glass:** the 2010 ADA Standards don't, to my knowledge, set a visual-marking rule for glass walls (building codes and safety-glazing rules handle it separately). Treat glazing as a hazard on orientation-and-mobility grounds, not as an ADA violation.

## Not hazards
Movable furniture (chairs, carts, trash cans) unless built in. Anything that reaches the floor in the walking line (the cane finds it), unless it is a column or a step. Things outside the walking line by more than about 1 m. Reflections, scan artefacts, ghosting.

## Confidence rubric
- **0.9-1.0:** clearly seen in a look, object named, clearly in or at the edge of the walking line, height plausibly in the band.
- **0.7-0.85:** clearly seen, but the height or distance from the walking line is estimated.
- **0.5-0.65:** probably real; the view is partly blocked or the angle is poor. Say why in the note.
- **Below 0.5:** don't confirm. Reject with the reason "uncertain" and list it so someone can re-check.

## Writing the note
It is spoken aloud during the walk, right before the traveller reaches it: "Caution: <note> ahead." Name the object and its height in plain words: "wall sconce at head height", "low sign overhead", "underside of a staircase, low headroom", "single step down", "glass wall". No metres, no left or right (the walking direction is unknown), no "you will see".
