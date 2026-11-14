# Demo script (3 minutes)

**[keys]** marks a step that needs real keys: Matterport or Vexcel from `/costar-keys`, `OPENROUTER_API_KEY` (free models), or `PHOTON_*`. Everything else runs on the synthetic building with no keys. Have the debug view open at http://localhost:8765/debug.html (`python -m uvicorn nudge.server:app --port 8765`) and iMessage on a phone that has been added to the Photon project.

## 0:00 The problem (20 s)

> "A blind visitor walks into an office building for the first time. GPS stops at the door. A cane finds what's on the floor but misses what sticks out at head height: wall sconces, counter overhangs, the underside of an open stair (ADA 307.2, 27 to 80 inches). Glass walls are nearly invisible to a 3D scan too. CoStar already has a digital twin, an aerial photo and a building record for the same address. Nudge turns those into guidance."

Show the README architecture diagram, one sentence per layer: offline agent, live router, exceptions.

## 0:20 The dossier agent (50 s) [keys: Matterport + OpenRouter for a real building]

```bash
python -m nudge.agents.dossier <sid>
```

The terminal streams the lead's plan and its delegations:
- `[lead] plan 0/9 done`
- `[lead] -> site-surveyor`
- `[lead] -> hazard-scout: floor ...`
- `[hazard-scout] overhang_candidates(floor=...)`
- `[hazard-scout] looks from ... at 76.5 deg`
- `[lead] writes /dossier/annotations.json`
- `[lead] audit_routes(profile=blind)`
- `[lead] -> landmark-writer: ...`

Talking points:
- **Planning:** `write_todos`.
- **Three subagents** via `task()`, each with its own tools and a look budget.
- **Real files on disk:** `/dossier`, `/memories` and a read-only `/agent` holding AGENTS.md and the skills.
- **It checks its own work:** `audit_routes` runs our router on what it wrote, and the lead re-delegates the gaps.

Fallback without keys: run it on `synthetic`, or show `cache/dossiers/<sid>/` from an earlier run.

## 1:10 Geometry proposes, vision confirms (25 s) [keys for a real building]

Open the hazard file `cache/dossiers/<sid>/hazards/<floor>.json`. Show one confirmed candidate with its `evidence.pano_id` and `bearing_deg`, then show the `look()` image the scout saw. The compass in the corner shows which way it was facing.

> "The point cloud found something at 1.2 to 1.5 m with nothing underneath, which is exactly what a cane misses. The agent looked and confirmed it's a wall sconce. Rejected candidates stay in the file with a reason."

Offline version: `look('synthetic', 'p_L5', 76.5)` and the synthetic sconce `oh-f1-0`.

## 1:35 The walk (35 s) [no keys]

In the debug view: synthetic, start K3, goal Conference Room, profile blind, then **Simulate walk**.
- **Speech:** "Route ready… Turn right… Walk about 9 steps…"
- **Haptics:** distinct vibration patterns for left, right and arrive (on Android Chrome).
- **Reroute:** drag the dot into the lobby and it says "You are off the route. New route…"
- **Accessibility:** switch to wheelchair and the route takes the elevator, never the stairs.

> "None of this calls a model. It's A* on the twin. It takes milliseconds and works with no wifi."

Device lines for the hardware, from the CLI or `device.encode_step`: `2,T,L,90,4.5` and `N,2.4,-15,TR,0`.

## 2:10 "The elevator is out" over iMessage (35 s) [keys: Photon + OpenRouter]

```bash
python -m nudge.photon_bot
```

On the phone:
1. "take me to the conference room at synthetic". The reply is the route summary and the first step, or the briefing if a dossier exists.
2. "the elevator is out of service". The exception agent compares options and replies something like "The elevator is out. The stairs are 20 feet away… Reply yes to take this route." The live plan has not changed yet.
3. "yes". The reroute is committed and the next step uses the stairs.

> "A Deep Agent with human-in-the-loop: the one tool that changes the plan pauses until the traveller says yes. For a wheelchair profile it would never offer the stairs. It would say there's no step-free route and suggest another entrance or asking staff."

## 2:45 Outside in: the entrance survey (15 s) [keys: Vexcel + CoStar kit]

Open `cache/dossiers/<sid>/site.md` and the aerial image. Show the accessible entrance, the curb ramp and drop-off, and the building facts from the CoStar record (year built, suites as destination aliases).

> "Vexcel gets you to the right door. Matterport gets you through it. CoStar tells us what's inside and when it was built."

## Close (5 s)

> "AI ahead of the walk. Deterministic during it. The traveller decides when plans change."
