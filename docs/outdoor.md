# Outdoor leg: Vexcel (or open data) outside, Matterport inside

Nudge guides door to door. Outside, the visitor walks a street-level graph in local east/north metres, where phone GPS works. At the building entrance, guidance hands over to the Matterport scan in the model frame. All distances, spoken and on the device, are in metres.

```
GPS (lat, lon) ──► outdoor graph (ENU m) ──► entrance portal ──► indoor graph (model frame m) ──► destination
                   OSM topology               georef: scan ↔ footprint
                   Vexcel DTM, detect, footprint, obliques
```

## Where each piece of data comes from

| What | Source | Module |
|---|---|---|
| Building footprint, the destination polygon | Vexcel `/structure/geometry/extract` (primary structure at the model's lat/lon) | `vexcel.footprint` |
| Ground elevation, every 2 m along each path | Vexcel `/dtm/elevation`, LINESTRING in, LINESTRING Z out | `vexcel.elevation`, `outdoor._slopes` |
| Running slope, steep (>5%, ADA 403.3) and too-steep (>8.33%, 1:12, ADA 405.2) tags | Computed from the Vexcel DTM | `outdoor._slopes` |
| Curb steps: a DTM jump ≥0.10 m that is sharper than 12% | Vexcel DTM, plus OSM `kerb=raised` / `barrier=kerb` | `outdoor._slopes`, `outdoor.build` |
| Curb ramps | OSM `kerb=lowered/flush`, plus Vexcel detect `"curb ramp"` | `outdoor.build`, `_apply_detections` |
| Obstacles near the path (pole, bollard, tree trunk, bench, hydrant, planter) become hazards | Vexcel `/elements/detect`, open-vocabulary, run on ortho imagery | `vexcel.detect`, `_apply_detections` |
| Painted crosswalks, which mark crossings | Vexcel detect `"crosswalk"` | `_apply_detections` |
| Entrance check: facade view at a candidate door | Vexcel `/oriented/extract` oblique (north/south/east/west), crosshair on the door | `vexcel.oblique_image`, `handoff.facade_view` |
| Pixel ↔ world on obliques | Vexcel `/oriented/transform-points` | `vexcel.pixel_to_world` / `world_to_pixel` |
| Walkable topology: footways, sidewalks, crossings, steps, street edges | OpenStreetMap via the Overpass API, cached in `cache/osm/`, generic User-Agent | `outdoor.overpass` |
| Crossing signals, entrance nodes | OSM `crossing=traffic_signals`, `entrance=*` | `outdoor.build` |
| Scan rotation and offset on the map | Registration of the scan outline against the Vexcel footprint | `georef.register` |

## Outside Vexcel coverage: open data (`opendata.py`)

The hackathon Vexcel keys only cover Houston and Los Angeles. Anywhere else, Vexcel answers `403 Unauthorized by Geofencing permission`. Apart from its data sources, the outdoor pipeline doesn't depend on location, so `handoff.load_site` swaps in open data automatically in two cases: the model's lat/lon is outside the Houston and LA boxes (`opendata.VEXCEL_BOXES`), or Vexcel refuses (401, 403 or no structure). `site.source` records `"vexcel"` or `"osm+usgs"`, and the first warning says why. Inside coverage, with Vexcel answering, nothing changes.

| What | Open-data source | Module |
|---|---|---|
| Building footprint | OSM `way["building"]` / `relation["building"]` (largest outer ring) within 80 m: the polygon containing the model's lat/lon, else the nearest. Same Overpass mirrors, cache and generic User-Agent as the topology. | `opendata.footprint` |
| Ground elevation | USGS 3DEP: the 3DEP ImageServer `getSamples` (`https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/getSamples`), 100 points per request, 2 at a time; a failed batch is retried, then split. If that fails, the Elevation Point Query Service (`https://epqs.nationalmap.gov/v1/json?x=lon&y=lat&wkid=4326&units=Meters`) is asked one point at a time. Sampled on a grid and interpolated bilinearly. Cached in `cache/usgs/`. | `opendata.USGSElevation` |
| Entrances | OSM `entrance=*` nodes (already in the Overpass query), plus the scan's entrance zone: places the catalog names "Entrance …", put on the footprint outline | `handoff._entrance_candidates` |
| Fit check image | A plain Pillow canvas, since there's no ortho to draw on: OSM footprint (white), registered scan walls (yellow) and hull (dim), OSM paths (grey; crossings orange), streets (dark grey), entrance candidates (cyan, best filled) | `opendata.overlay_png` |

**It works anywhere OSM maps footways and buildings. Elevation comes from USGS, so it's US only.** Elsewhere, USGS returns no data, `build()` drops slopes with the warning "no elevation data … slopes unknown", and door steps stay `"unknown"`.

For any sid with a geolocation: `python -m nudge.opendata SID`. It prints the fit and the entrances and writes `cache/bringup/<sid>_georef_osm.png`. `--open` forces open data inside Vexcel coverage too, for comparison.

USGS is slow: a request takes 12–18 s whether it carries one point (EPQS) or 100 (`getSamples`), and more than two at once draws 502s. So the grid starts at 4 m and doubles until the graph needs at most 3000 samples (`MAX_GRID`). Rabinowitz Hall ended up on an 8 m grid: 2,146 samples, about 4 minutes on the first build, because OSM ways that touch the 150 m radius bring their full length. The first build pays for the samples once; after that it reads the cache.

**Lower fidelity than LA (Vexcel).** Say so when demoing:
- **No facade check.** There are no obliques, so `facade_view` has nothing to show. Entrances stay `confirmed: false`, with `evidence.basis` set to `"osm entrance tag"` or `"indoor entrance zone"`. The transition says so out loud: "This entrance comes from the scan's entrance area, placed on the map outline; it was not confirmed from imagery."
- **No detections.** `/elements/detect` is Vexcel-only, so there are no pole, bollard or tree hazards and no painted-crosswalk marking. Crossings come from OSM tags only.
- **Coarser elevation.** USGS 3DEP is 1 m lidar where it exists, but we sample it on a 4–32 m grid (8 m at Rabinowitz), so slopes average over that run. A curb (a 10 cm jump within 2 m) can't be seen; curbs come only from OSM `kerb=*` (`outdoor._slopes` skips DEM curbs when the elevation is gridded). For the same reason, the step at the door is always `"unknown"` and the transition makes no step claim. Mapped OSM steps on the way are still announced ("steps up").
- **Volunteer footprint.** OSM building outlines are traced by volunteers. They're usually within a metre or two, but some include arcades and some don't.
- **Partial scans.** A scan of part of a big building can't use the hull fit. It uses the wall fit described below, which is looser along the building's length.

**Rabinowitz Hall (R1FX6EmdcDt), Princeton.** Run with `python -m nudge.opendata R1FX6EmdcDt`; the image is `cache/bringup/R1FX6EmdcDt_georef_osm.png`.
- **Footprint.** OSM `way/1086964029` "Julis Romo Rabinowitz Building", an H about 50 m across.
- **Fit.** The scan's ground floor is a cross: a 57 m corridor, an atrium and an east corridor. It fits inside the footprint at 29.0°, at 0.71 m rms on the 9% of the outline its walls touch, with no walls outside. The long corridor runs along the building's long axis (NNW–SSE). The east corridor ends at the east face.
- **Entrance.** The "Entrance arcade" lands on the west face, where an OSM footway from Washington Road meets the facade. The 209° fit scores almost as well but puts the entrance on the east face, 69 m from a main road against 33 m for the west face, so 29° is kept.
- **Door to door.** From OSM node 8948919411 on the Washington Road footway, 54 m from the door: 68.7 m outside ("Walk 50 meters, then turn left. Steep slope up." / "Walk 19 meters to the main entrance. Steep slope up; steps up; steep slope down."), then 15.9 m inside to the Grand staircase, or 30.8 m (up the stairs) to the Seminar room.

**Why OSM for topology.** Vexcel's `/elements/detect` is open-vocabulary detection: it returns polygons for a text label such as "crosswalk" or "pole". It does not return a routable sidewalk network, and turning sidewalk masks into a graph (skeletonizing, then joining across occlusions) is a project of its own. So OSM supplies the lines and Vexcel supplies everything we measure on them. On a street with no mapped sidewalk, OSM street edges are used with a `roadway` tag, which every profile penalizes heavily.

## Profiles outside

`outdoor.profile(name)` merges the indoor profile with outdoor penalties. It returns a `router.Profile`, so `plan()` and `Navigator` take it unchanged.

| tag | default | blind | wheelchair |
|---|---|---|---|
| `curb` (step, no ramp) | +5 m | +30 m | **avoid** |
| `too_steep` (>1:12) | +15 m | +2 m | **avoid** |
| `steep` (>1:20) | +5 m | +0.5 m | +20 m |
| `crossing` | +5 m | +25 m | +5 m |
| `unsignalised` | +5 m | +40 m | +10 m |
| `roadway` (no sidewalk mapped) | +10 m | +60 m | +40 m |
| `stairs` (OSM steps) | inherits indoor | +8 m | **avoid** |
| `hazard` (detected obstacle) | inherits indoor | +40 m | +10 m |

## Putting the scan on the map (`georef.py`)

The Model API gives one lat/lon per model and no rotation. `register()`:
1. Takes the scan's outer outline: the convex hull of the ground floor's wall vertices.
2. Samples the outline and the Vexcel footprint every 0.25 m.
3. Grid-searches rotation in 2° steps, with translation from aligning centroids, scored by a truncated symmetric chamfer distance.
4. Refines the 4 best distinct starts with point-to-segment ICP, using SVD with no reflections and trimming at 1.5 m.
5. Returns `theta_deg, tx, ty, rmse_m, inlier_frac, ok`, plus `model_to_enu` / `enu_to_model` / `lonlat_to_model` and a compass-to-model heading helper.

A mirrored fit is tried only when the plain fit is poor (rmse above 1 m). For symmetric footprints such as rectangles, `ambiguous` is true and `alternatives` lists the other fits (for example θ+180°). The entrance confirmation below settles it. For the 232 splat models, `drone_anchor(sid)` gives the mean GPS of the drone photos as a translation prior.

The synthetic test recovers a 30° / (35, 20) m transform with 8 cm of noise to within 0.2° and 1 cm.

**Partial scans (`register_partial`).** A scan of part of a big building has a hull that isn't the footprint. At Rabinowitz Hall, the scan is a cross of corridors inside an H-shaped footprint, so the hull fit lands at rmse 3.6 m with alternatives no better. When the hull fit isn't `ok`, `georef_model` registers the walls of the georef floor instead:
- **Rotation.** Rectilinear buildings line their walls up with the footprint edges mod 90°. When both direction histograms are peaked, only those four turns (±2°) are tried; otherwise every 2°.
- **Translation.** An FFT over the whole footprint maximizes walls on the footprint edge and penalizes walls more than 0.5 m outside it. There's no centroid assumption.
- **Reported values.** `rmse_m` is measured over the walls that touch the outline. `inlier_frac` is the share of the outline they cover.
- **When it counts.** The fit is `ok` when it's tight (≤1.5 m) along at least 20 m of the outline with under 3% of walls outside. Only then does it replace the hull fit, and it's tagged `source: "footprint-partial"` with a warning.
- **Alternatives.** Alternatives carry their own translation, so `resolve_flip` can try the 180° fit and keep whichever puts the entrance on the street.

**Which floor.** `handoff.georef_floor` picks it: the lowest floor, unless that floor has no panos (Rabinowitz's unscanned lower level); then the floor with the entrance zone.

## Entrances (`handoff.py`)

`entrance_candidates(indoor, georef, outdoor, sid)` is deterministic:
1. **Outer doors:** passable wall edges (door/opening) within 0.5 m of the scan's outer outline, attached to the nearest pano. The door edge length gives `width_m`.
2. **Entry rooms:** panos in rooms labelled entrance, lobby, vestibule, foyer or reception, with the door point taken where the room meets the outline.
3. **Entry zones:** panos the place catalog names as an entrance, such as "Entrance arcade". Matterport often labels these rooms "Other". A zone counts if it lies within 12 m of the outline, and its door is the footprint edge nearest the pano.

Each door point goes through georef into ENU, snaps onto the footprint edge (Vexcel or OSM), then snaps to the nearest outdoor node within 15 m. Candidates are ranked by:
1. OSM `entrance=*` on that node;
2. a door opening onto an entry room;
3. an entry-labelled pano;
4. outer door before entry room or zone;
5. room label;
6. snap distance.

Unconfirmed candidates carry `evidence.basis`: `"osm entrance tag"` or `"indoor entrance zone"`. `transition_text` says it in plain words.

Each record:

```json
{"outdoor_node": "out:140", "sid": "...", "pano_id": "p_L1", "kind": "entrance",
 "width_m": 0.92, "step": false, "confirmed": false, "evidence": {},
 "room": "Lobby", "source": "outer_door|entry_room|entry_zone", "faces": "south",
 "door_enu": [x, y], "door_lonlat": [lon, lat], "snap_m": 1.7, "osm_entrance": true}
```

`step` is true, false or `"unknown"`. It compares DTM ground at the door with the approach node: false if they differ by under 3 cm, true if they differ by 12 cm or more at a grade steeper than 1:12. The DTM cannot see a threshold lip, so "unknown" is common and honest.

`plan_door_to_door(start_latlon, sid, goal, profile)` routes:
1. outside, from the nearest outdoor node to the best portal (confirmed first);
2. a transition at the door, such as "You're at the main entrance. The door is 0.9 meters wide, no step. Going inside.";
3. inside, from the portal pano to the goal.

It returns one maneuver list (each item tagged `leg`), the two routes, and device lines per leg: the band hears `A` at the door, then a fresh `D` inside. `DoorToDoorNavigator` does the same live. It takes `update(lat=, lon=, compass_deg=)` outside and `update(x=, y=, floor=)` inside; GPS indoors is converted through georef as a rough fallback.

## Agent prompt: the site-surveyor's entrance check

Give the site-surveyor these tools: `handoff.OUTDOOR_TOOLS` = `entrance_candidates_tool`, `facade_view`, `confirm_entrance`. Add this to its prompt:

> **Entrance check.** Call `entrance_candidates_tool(sid)`. If it lists no candidates, say so in /dossier/site.md and stop; never invent an entrance. For the top 3 candidates, call `facade_view(sid, i)` with the side the facade `faces`, and the opposite side if the door is not visible. Budget: 6 views total. In each image, look at the crosshair:
> - Is there a door there?
> - Is it level with the sidewalk, or does it have steps? Is there a ramp, and where?
> - Is it automatic, revolving, a push door, or a gate?
> - Does it look like the main public entrance (canopy, signage, glass doors) or a service door?
>
> Then call `confirm_entrance(sid, pano_id, accessible, note)` once for the entrance a blind or wheelchair visitor should use. `accessible=false` means steps with no ramp. You may only pick a `pano_id` from the candidates; the tool rejects anything else. Coordinates come from the tools, never from you. If `georef.ambiguous` is true, say which facade the door you confirmed is on; that settles the scan's rotation. Write what you saw, with the image direction used, into /dossier/site.md under "Entrance".

## Wiring left to the main thread (files this fork did not own)

1. **Site-surveyor tools.** In `nudge/agents/tools.py`:
   ```python
   from ..handoff import OUTDOOR_TOOLS
   SITE_TOOLS = [building_overview, aerial_view, costar_record, list_panos, look] + OUTDOOR_TOOLS
   ```
   Then append the "Entrance check" prompt above to `nudge/agents/prompts/site-surveyor.md`. There is no circular import: `handoff` only imports `agents.tools` inside functions.
2. **Server endpoints** (`nudge/server.py`):
   - `GET /api/outdoor/{sid}/site`: `{georef, portals, warnings, outdoor: og.to_dict(), footprint_enu}` from `handoff.load_site(sid).`
   - `POST /api/outdoor/plan {sid, lat, lon, to, profile}`: `handoff.plan_door_to_door(...)`.
   - `POST /api/outdoor/nav/start {sid, to, profile, lat, lon, compass_deg?}`: create a `DoorToDoorNavigator` and return `{session, events}`.
   - `POST /api/outdoor/nav/update {session, lat?, lon?, x?, y?, floor?, compass_deg?}`: return `{events, phase}`.
3. **BuildingGraph (task A).** The outdoor graph is a normal `WalkGraph` with floor `"outdoor"`, node ids `out:<osm id>`, `og.enu` and `og.footprint`. Add it as one more map, keyed e.g. `"outdoor"`, joined to the scan by a portal `(outdoor_node, sid:pano_id, kind="entrance", cost_m=snap_m)` taken from `entrance_candidates`. The transition maneuver text is `handoff.transition_text(portal)`.
4. **Router defaults (optional).** Merging `outdoor.OUTDOOR` penalties into `router.PROFILES` would let a plain profile name work outside too. Today, pass `outdoor.profile(name)` instead.
5. **Device (optional).** A dedicated "switch map" act letter would let the band pulse distinctly at the door. Today it gets `A` (arrive) and then a new route starting `D`.

## Needs real keys to check

- What `/elements/detect` returns for "pole", "crosswalk" and "curb ramp" on 7.5 cm ortho: response shape and quality. The client parses features, detections, elements and results leniently.
- DTM resolution near curbs: the curb heuristic needs a sharp jump within one 2 m sample.
- Which oblique `product-type` shows a south-facing facade best. The tool falls back to trying all four.
- The overall georef error on real scans. Partial scans (a single suite, or Rabinowitz's corridors) cover only part of the footprint. A small one is handled by the truncated chamfer and trimmed ICP. A big one falls through to `register_partial`, which is looser along the building's length. `ok` and `inlier_frac` report both cases.
