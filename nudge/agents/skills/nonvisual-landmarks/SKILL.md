---
name: nonvisual-landmarks
description: How to choose and phrase landmarks a blind traveller can use, following orientation and mobility (O&M) practice - underfoot, touch, sound, air - with good and bad examples, egocentric phrasing and clock-face directions. Use when writing or reviewing landmarks.
---

# Non-visual landmarks

## What O&M instructors mean by a landmark
A **landmark** is a permanent, recognisable feature that tells a traveller exactly where they are. A **clue** is anything that helps but can change (a smell, a noise from a room, a door that is sometimes open). Good routes are built on landmarks, with clues as backup. Travellers also use **shorelines**: a wall edge, a carpet edge, or a grass line they can trail with the cane to stay oriented.

What makes a landmark good:
1. **Permanent.** Built in, not furniture.
2. **Detectable without sight**, by cane, foot, hand, ear or skin.
3. **Unique** on that stretch: the only texture change in the corridor, not the fifth identical doorway.
4. **At the decision point** (within about 1-2 m of where the turn happens), not halfway down the corridor.
5. **Safe to find**: don't send someone feeling along a wall toward a protruding object.

## By sense, from most to least reliable
- **Underfoot.** Changes of floor material are the strongest cue: carpet to tile, tile to wood, a rubber entrance mat, a metal grate, a threshold strip. A cane hits them and a foot feels them. From a pano, look down and compare the floor near the pano with the floor in the direction of travel.
- **Touch (cane or hand).** A wall that ends (the corridor opens up), an inside or outside corner, the start or end of a handrail, a column, a counter edge, a planter edge, door hardware (push bar versus lever versus a pull handle), an elevator call button panel, a drinking fountain.
- **Sound.** An elevator chime, a fountain, a loud HVAC vent, an open atrium (echo and space) versus a quiet carpeted corridor, a kitchen or break room, an automatic door motor. Good as backup; can vary by time of day.
- **Air.** A draught from an exterior door, warm or cool air at a vestibule, a strong vent.
- **Smell.** Coffee, a kitchen, cleaning products. Only as a secondary clue.

## Never use as the only cue
Colours, sign text, artwork, logos, "the glass room", lighting, anything described with "see" or "look". A low-vision visitor may use them, but your text is for someone who can't.

## Good versus bad
| bad | good |
|---|---|
| "the blue sign" | "carpet changes to tile underfoot" |
| "a painting on the wall" | "the wall ends at a corner" |
| "the reception desk" (as a sight cue) | "a counter edge at waist height on your side" |
| "a chair by the door" (movable) | "push-bar double doors" |
| "an office" | "elevator chime ahead" |
| "the hallway" | "the corridor narrows and becomes carpeted" |

## Phrasing
Your text is spoken after "Landmark:" right at the turn, so make it a short noun phrase or clause, under about 10 words, in the present tense:
- "carpet changes to tile underfoot"
- "handrail starts here"
- "the wall ends at an open area"
- "rubber mat underfoot, exterior doors ahead"

**Egocentric directions** (left, right, ahead, behind) depend on the walking direction, which you don't know when writing the dossier. So prefer cues that don't need a side ("underfoot", "ahead", "the wall ends"). Store the direction as `bearing_deg` in the model frame, and the navigator can turn it into left, right or a clock direction at walk time.

**Clock-face directions** are standard in O&M: 12 o'clock is straight ahead, 3 o'clock right, 9 o'clock left, 6 o'clock behind. The navigator converts a bearing into a clock direction when speaking; don't write clock times into the dossier.

## Choosing at a pano
1. Look at the four horizon headings and down.
2. List the candidate cues by sense.
3. Drop anything movable, visual-only, or more than about 2 m from the pano.
4. Keep the best 1-2, preferring an underfoot change, then a touch cue, then sound.
5. If nothing qualifies, mark the pano as having no good landmark. An honest gap beats a useless cue.
