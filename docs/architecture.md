# Architecture

Isaac has three layers. Only the first and third use a language model, and neither runs inside the walk.

1. **Offline dossier.** A Deep Agent surveys one building and writes `annotations.json`: hazards, landmarks and tags.
2. **Live navigation.** A deterministic walk graph, A*, maneuvers and device packets. No model in the loop.
3. **Exceptions.** A Deep Agent with human-in-the-loop that proposes reroutes through the same router.

## Why routing is deterministic

- **Speed.** A route on a few hundred panos takes milliseconds, and live cues need that. A model call takes seconds and depends on the venue's wifi.
- **Correctness.** Left versus right is the sign of a cross product, and distance is a sum of edge lengths. Those are tested against hand-worked cases in `tests/test_device.py`. A model can mix up left and right or invent a doorway; A* can't.
- **Debuggability.** Every route can be replayed, unit-tested and shown in `web/debug.html`.

The model's job is the part geometry can't do: deciding that a point-cloud blob at 1.3 m is a wall sconce and not a shadow, describing the carpet-to-tile change you'll feel underfoot, and choosing between a 200 ft accessible detour and a different entrance.

## Data contracts

### Model JSON (`nudge/fetch.py`)

There is one Model API query (`Q_WALK`), cached as `cache/models/<sid>.json`:

```
model { id name internalId address{address locality} geolocation{lat long}
  floors { id label sequence vertices{id position{x y}} edges{id type thickness vertices{id}} }
  rooms { id floor{id} classifications{label confidence} boundary{edges{id}} dimensions(units: metric){...} }
  panoLocations { id label position{x y z} floor{id} room{id} } }
```

`sequence`, `thickness`, `dimensions`, `internalId`, `panoLocations.floor` and `panoLocations.room` are not confirmed in the schema. If the API rejects one, `load()` strips it and retries, and records it under `_dropped`. The `sid` `"synthetic"` returns the two-floor building in `tests/synthetic.py` without touching the network.

### Walk graph (`nudge/walkgraph.py`)

- **Nodes are scan positions.** A tripod stood there, so they're walkable. Each has id, label, x/y/z (metres, model frame, z up), floor, room and room label, tags and landmarks.
- **Edges** link two panos on the same floor when the segment between them is at most 5 m (8 m for an isolated pano) and crosses no blocking wall. Each pano keeps its 6 nearest visible neighbours.
  - Wall edges whose `type` is one of `opening`, `door`, `doorway`, `entrance`, `entry`, `archway`, `open` or `passage` are passable, and the edge is tagged `door`.
  - Every other type blocks, including a missing type.
  - An edge whose ends are in different rooms gets `room_change`.
- **Floors** are joined through panos in rooms labelled stair/staircase/stairwell/steps or elevator/lift, within 5 m horizontally. The edge is tagged `stairs` or `elevator` plus `vertical`, and its length is |dz| plus the horizontal distance.
  - With no such room, the nearest pair is linked and tagged `vertical_unknown`, with a warning.
  - If panos carry no floor id, floors are assigned by splitting the z values at the largest gaps.
- **Disconnected pieces** are bridged to the main component through the shortest crossing of at most 4 m. These edges are tagged `uncertain`, with a warning.
- **Sweep neighbours** from the Showcase SDK, when present, are added as `sdk` edges.

`WalkGraph.to_dict()` is what the server sends to the browser: `floors[{id,label,walls[[a,b,type]]}]`, `rooms{id:{label,floor,poly}}`, `nodes[...]`, `edges[{a,b,length,tags,hazards}]` and `warnings`.

### annotations.json (dossier → router)

This is written by the dossier lead at `/dossier/annotations.json`, which is `cache/dossiers/<sid>/annotations.json` on disk. `walkgraph.apply_annotations` reads it, and the `annotation-format` skill documents it for the agent.

```json
{
  "nodes": {"<pano_id>": {"tags": ["closed"], "landmarks": [
      {"text": "carpet changes to tile", "sense": "underfoot", "pano_id": "...", "bearing_deg": 90}]}},
  "edges": {"<a>|<b>": {"tags": ["narrow"]}},
  "hazards": [{"x": 8.0, "y": 2.5, "floor": "<floor_id>", "radius": 0.4, "kind": "protruding_object",
               "note": "wall sconce at head height", "z_low_m": 1.2, "z_high_m": 1.5, "confidence": 0.9,
               "evidence": {"pano_id": "...", "bearing_deg": 0, "candidate_id": "oh-f1-0"}}]
}
```

- **Node tags** are added to the pano. `closed` excludes it in every profile; `stairs`, `elevator` and `step` are understood too.
- **Edge tags** (keys in either order) are added to the edge: `step`, `narrow`, `glass_wall`, `uncertain`.
- **Hazards** tag every same-floor edge whose segment passes within `radius` of (x, y) with `hazard`, and attach the hazard object. Edges listed in the hazard's `on_edges` (or `evidence.on_edges`, copied from the point-cloud candidate) are tagged too, even when they fall outside the radius. The navigator speaks its `note` when the user gets within 3.5 m.
- **Landmarks** are spoken at the maneuver for that pano. Underfoot is preferred, then touch, sound and air.

`tools.validate()` checks this file against the walk graph: real pano ids, real edges, valid floors, known kinds and senses, radius in (0, 3] m, and `evidence.pano_id` present. It then reports which blind and wheelchair routes changed.

### Profiles (`nudge/router.py`)

A* runs over the state (node, previous node), so turning can cost something. The heuristic is straight-line distance plus 2 m per floor change, and every penalty is non-negative, so the heuristic stays admissible.

| Profile | Never uses | Extra metres per use | Turn cost |
|---|---|---|---|
| default | `closed` | uncertain 15, vertical_unknown 30, hazard 5, narrow 2, glass_wall 2 | none |
| blind | `closed` | hazard 40, uncertain 40, vertical_unknown 60, stairs 8, door 1.5, room_change 0.5, glass_wall 15, narrow 4, step 10 | 2 m per 90° above 20° |
| wheelchair | `stairs`, `closed`, `step` | hazard 10, uncertain 50, vertical_unknown 200, door 3, narrow 25, glass_wall 3 | none |

Goals can be given as a pano id, a pano label, a room id or a room label (exact match, then substring), or as landmark text.

### Maneuvers (`nudge/instruct.py`)

The route is split into same-floor legs and floor changes. Each leg is simplified with Douglas-Peucker (0.6 m) so pano jitter doesn't become a turn, and each remaining vertex is classified by its signed angle (+ is left): straight under 25°, bear under 55°, turn under 135°, otherwise a U-turn.

Each maneuver has these fields:
- `type`: depart, turn, bear, uturn, straight, walk (to stairs or elevator), floor, or arrive
- `side`, `turn_deg`, `node`
- `along`: cumulative route metres
- `walk_along`: walking metres only, excluding elevators and stairs
- `distance`: metres since the previous maneuver
- `text`: spoken; doorways, the room being entered, the best landmark and hazard warnings are folded in
- `cue`: a vibration pattern name from `CUES`
- floor maneuvers also have `via`, `vertical_m`, `rise_m` and `to_floor`

`orient()` turns the user's heading into "Turn left, toward 9 o'clock."

### Navigator (`nudge/navigator.py`)

- **Start.** It snaps the user to a nearby pano with a clear line of sight. Among the visible panos within 2 m of the nearest one, it picks the one that makes the whole trip cheapest, so a reroute never starts by walking you backwards.
- **Tracking.** Each position update is projected onto the route.
- **Cues.** "In N feet" at 8 m (skipped when the leg instruction just said it), "Turn left now" at 2 m, and arrival within 1.2 m.
- **Off route.** Two updates more than 2 m from the route, or with a wall between the user and the route, trigger a reroute. `block()` blocks a node or edge and replans.
- **Device numbers.** `live()` gives the metres to the next maneuver, and the signed angle from the user's heading to a point 1.5 m ahead on the route. That point never lies across an elevator ride.

### Device packets (`nudge/device.py`)

- Metres are horizontal walking metres, to 0.1. Degrees are signed integers in the model frame, + is left.
- Lines are ASCII and always under 20 bytes:
  - route step `i,ACT,DIR,deg,m`, e.g. `2,T,L,90,4.5`
  - floor step `i,F,U|D,E|S|X,floor#`
  - live `N,m_next,deg_next,ACTDIR,flags`, where flags add up: 1 off route, 2 arrived, 4 unmapped, 8 hazard ahead; route steps also include an `S` step at every doorway middle
- `encode_*` and `decode_*` round-trip.

## Dossier agent (`nudge/agents/dossier.py`)

`create_deep_agent` is configured with:
- the lead tools `building_overview`, `list_panos`, `audit_routes` and `validate_annotations`;
- three subagents with their own tools and prompts from `nudge/agents/prompts/`:
  - `hazard-scout`: `look`, `overhang_candidates`, `glass_suspects`
  - `landmark-writer`: `look`
  - `site-surveyor`: `aerial_view`, `costar_record`, `look`
- `TodoListMiddleware`, `memory=["/agent/AGENTS.md"]` and `skills=["/agent/skills/"]`;
- a `CompositeBackend`:

| Agent path | On disk | Purpose |
|---|---|---|
| `/dossier/` | `cache/dossiers/<sid>/` | output: `hazards/<floor>.json`, `landmarks/<floor>.json`, `site.md`, `annotations.json`, `audit.md`, `briefing.md` |
| `/memories/` | `cache/memories/` | `lessons.md`, carried across buildings |
| `/agent/` | `nudge/agents/` | AGENTS.md, prompts, skills; write denied by `FilesystemPermission` |
| anything else | agent state | scratch |

Because `/dossier/` is a real directory, the router reads exactly what the agent wrote: `tools.graph(sid)` rebuilds the walk graph with `annotations.json` applied. The lead closes the loop by running `audit_routes` on its own output. That audit routes from the entrance to every labelled room and lists unreachable rooms, uncertain links, hazards on routes and turn points with no landmark. The lead then re-delegates narrow fixes.

### Perception (`nudge/perceive.py`)

- **`look(sid, pano, yaw, pitch, fov)`** reprojects the six skybox faces into a perspective view and stamps a compass and the yaw on it. Faces are cached under `cache/panos/`. For `synthetic` it renders a ray-cast view of the wall graph instead.
- **`overhang_candidates(sid, floor)`** takes the MatterPak `cloud.xyz` (every 10th point, cached as `.npy`) and estimates a floor plane per floor. On a 0.1 m grid it keeps cells with points in the 0.685–2.03 m band and nothing in the 0.05–0.685 m band below them or their neighbours, within 1 m of a walk edge, then clusters them. Each candidate comes with the nearest pano that has line of sight to it, and the bearing to look along.
- **`glass_suspects(sid, floor)`** flags wall edges whose point support at 0.5–2.0 m is under 0.35 of the floor median.
- **`aerial(sid)`** returns the Vexcel ortho with a north arrow and scale bar.
- **Calibration needed:** `FACE1_YAW_DEG` and the per-pano rotation field must be calibrated with `calibrate()` on real data.

## Exception agent (`nudge/agents/exception.py`)

Its tools are bound to one live `Navigator`:
- `nav_status`
- `list_nearby`
- `compare_options`: a what-if `router.alternatives` with a hypothetical block applied; it never mutates the live plan
- `alternate_goals`: rooms that serve the same need, through a synonym map
- `commit_reroute`

`interrupt_on={"commit_reroute": True}`. `handle_event()` returns lines to say plus the pending action. `approve(..., "approve"|"reject")` resumes with `Command(resume={"decisions": [...]})`. The system prompt forbids inventing geometry and treats profile rules as hard constraints.

## Room changes, multi-scan buildings, unmapped areas

The rule is the same everywhere: agents decide *what* (what to survey, which portal joins two scans, what to do off the map); the router and the scan geometry produce every metre and degree the device follows.

### Room changes (`walkgraph.shape_edges`, `instruct.path` / `door_points`, `navigator`)

- **Doorway waypoints.** Every link that crosses a door or opening gets waypoints at the door's middle, plus one 0.5 m before and after it on the door's normal. The walking line goes through the middle of the opening, square-on, instead of clipping a jamb. Each link stores `doors: [{x, y, width}]`, where width is the passable edge length, and its `length` becomes the polyline length.
- **Door width.** Under 0.815 m (32 in clear width, ADA 404.2.3), the link is tagged `narrow_door`, and the wheelchair profile treats that as a hard stop. Under 0.9 m, the spoken text says "through a narrow doorway".
- **Wall clearance.** Any stretch of the line within 0.35 m of a blocking wall gets a single waypoint that pushes it away, but only if the new line stays clear (checked with `blocked_line`). If that fails, the link is tagged `tight`; profiles penalise it, and wheelchair most of all.
- **Live tracking.** The navigator tracks along that exact polyline. The lookahead bearing (`live()`) therefore steers through the door middle.
- **Live events.**
  - `door` fires about 2 m before each door middle ("Doorway ahead, 2 meters").
  - `room` fires when the room changes. The room comes from the polygon containing the position, else the nearest pano's room, and needs two fixes in a row. The text is "Entering the kitchen." plus the best non-visual landmark at the entry pano.
- **Device.** The route packet gets an extra `S` step (deg 0) at each door middle, so the next step's metres count from the door.

### Multi-scan buildings (`nudge/building.py`)

- **Why portals.** A building is often several Matterport models: a lobby scan, one scan per floor, suites. They share a `costar_property_id` or a street address (`siblings`), and each has its own frame. Nothing is aligned. `merge()` places the per-scan walk graphs side by side, with ids `sid:pano`, floors `sid:floor`, and each floor's `frame` set to its sid. It joins them only through **portals** (`elevator|stairs|door|entrance`, `cost_m`, `direction`, `note`).
- **Routing across portals.** Portals are tagged `vertical`. To the router, maneuvers and navigator they are floor changes: "Take the elevator up to 35th Floor", or "Go through the lobby doors into …". Back-to-back rides merge into one maneuver. The A* heuristic is 0 across frames, so it stays admissible.
- **Finding portals.** `candidates_portals()` proposes pairs from room labels, scan names and floor numbers. The `building-cartographer` subagent confirms or rejects each pair with `look()` at both ends and writes `/dossier/portals.json` (`validate_portals` checks it). `building_graph(sid)` merges only the scans that confirmed portals reach.
- **Positions.** The navigator takes `(x, y, floor, sid=...)` and namespaces the floor itself.

### Unmapped areas (`navigator`, `agents/explorer.py`)

- **Detection.** A position is off the map when its floor isn't in the map, when no pano is reachable in a straight line within 6 m, or when it is outside every room polygon and more than 3 m from the route.
- **Behaviour off the map.** The navigator stops all turn instructions and emits `unmapped`.
  - If the last on-map position is a plausible walk away, it offers the way back: your own breadcrumb trail, reversed (`way_back()`).
  - Otherwise, or past 30 m of wandering, it says: "This area isn't in my map. Please stop and use your cane; I can call for help."
- **Back on the map.** It emits `remapped` and a fresh route from the current position.
- **Device flag 4 (unmapped).** `m`/`deg` point back along the trail, or are empty if there's no safe way back. The band plays slow 600 ms pulses on both motors and repeats them every 4 s. Flag 8 is "hazard ahead".
- **The explorer agent.** A Deep Agent that runs on `unmapped`, or when the destination has no scan or dossier.
  - It plans with `write_todos`.
  - Its tools include `explorer_status`, `dossier_status`, `quick_survey`, `scan_portal_candidates`, `aerial_view`, `costar_record` and `compare_options`.
  - It delegates to `hazard-scout`, `site-surveyor` and `building-cartographer`.
  - Its only acting tool, `commit_plan`, is behind HITL. It can set a destination the router resolves, add a *candidate* portal (tagged `uncertain` until confirmed), or block a pano. It never hands over coordinates.
- **Route-scoped quick dossier.** `dossier.run(sid, focus=..., goal=...)` (or `python -m nudge.agents.dossier <sid> --goal Kitchen`) surveys only the panos within 3 m of the route a first visitor will walk (`tools.route_focus`), hazards first. It writes `annotations.json` early so the router uses the findings within minutes, then carries on with the full survey.

## Channels

- **`nudge/server.py`** is FastAPI with:
  - `GET /api/graph/{sid}`, `POST /api/route`
  - `POST /api/nav/start|update|block`, where each event carries its vibration pattern
  - static `web/debug.html`
- **`nudge/photon_bot.py`** has two parts:
  - **`Brain`** is channel-agnostic: help, a profile, "take me to X at Y", next, "where am i", stop, yes/no for a pending reroute, and anything else goes to the exception agent. On a building with no dossier, it starts the dossier agent in a background thread.
  - **`PhotonChannel`** polls `list_messages`, sets the typing indicator, and replies with `send_message` (idempotency key `nudge:<message id>:<n>`) in the same space.
- **`langgraph.json` / `nudge/agents/graphs.py`** serve both agents for `langgraph dev`. Studio can't pass a building at build time, so the graphs are built for `NUDGE_SID` (default `synthetic`). The exception graph gets a navigator started at that building's entrance, heading for `NUDGE_GOAL` with `NUDGE_PROFILE`.
