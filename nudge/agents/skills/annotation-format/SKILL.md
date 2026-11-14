---
name: annotation-format
description: The exact schema of /dossier/annotations.json that the deterministic router reads (walkgraph.apply_annotations), with a full valid example, the tags the router understands, and common mistakes. Use before writing or merging annotations.json.
---

# annotations.json

The router reads this file with `nudge.walkgraph.apply_annotations(graph, ann)`. Anything outside this schema is ignored; anything malformed inside it can silently do nothing. Always run `validate_annotations(sid)` after writing.

## Schema
```
{
  "nodes": {
    "<pano_id>": {
      "tags": ["step" | "closed" | "stairs" | "elevator" | ...],
      "landmarks": [
        {"text": str, "sense": "underfoot|touch|sound|air|smell", "pano_id": str, "bearing_deg": number|null}
      ]
    }
  },
  "edges": {
    "<pano_a>|<pano_b>": {"tags": ["door" | "hazard" | "uncertain" | "step" | "narrow" | "glass_wall"]}
  },
  "hazards": [
    {"x": number, "y": number, "floor": "<floor_id>", "radius": number,
     "kind": "protruding_object|glass_wall|glass_door|open_stair_underside|low_overhang|floor_level_change|drop_off|column|other",
     "note": str, "z_low_m": number|null, "z_high_m": number|null, "confidence": number,
     "evidence": {"pano_id": str, "bearing_deg": number, "candidate_id": str|null}}
  ]
}
```

## What the router does with each part
- **Node tags** are added to the pano. `closed` means don't route through it (the blind and wheelchair profiles avoid it). `stairs` / `elevator` mark vertical connectors. `step` means a level change at that spot (the wheelchair profile avoids it).
- **Node landmarks** are spoken at turns ("Landmark: ..."). Underfoot cues are preferred first, then touch, sound, air.
- **Edge tags** are added to the link between two panos. The router penalises `hazard`, `uncertain` and `door`, and the wheelchair profile avoids `step`. `narrow` and `glass_wall` are recorded for the audit and briefing but carry no routing cost today.
- **Hazards** are matched to every same-floor link that passes within `radius` metres of (x, y), plus any links listed in `on_edges` (`[[pano_a, pano_b], ...]`, copied from the geometry candidate). Those links get the `hazard` tag and carry the hazard, so routes avoid them when there is a reasonable detour, and the navigator says "Caution: <note> ahead" when the traveller gets close.

## Full valid example
```json
{
  "nodes": {
    "p_L6": {
      "tags": [],
      "landmarks": [
        {"text": "carpet changes to tile underfoot", "sense": "underfoot", "pano_id": "p_L6", "bearing_deg": null},
        {"text": "the wall ends at an open area", "sense": "touch", "pano_id": "p_L6", "bearing_deg": 90}
      ]
    },
    "p_K1": {"tags": ["step"], "landmarks": []},
    "p_S1": {"tags": ["stairs"], "landmarks": [{"text": "handrail starts here", "sense": "touch", "pano_id": "p_S1", "bearing_deg": 0}]}
  },
  "edges": {
    "p_L4|p_O1": {"tags": ["glass_wall"]},
    "p_O4|p_E1": {"tags": ["narrow"]}
  },
  "hazards": [
    {"x": 11.2, "y": 2.9, "floor": "f1", "radius": 0.6, "kind": "protruding_object",
     "note": "wall sconce at head height", "z_low_m": 1.25, "z_high_m": 1.5, "confidence": 0.85,
     "evidence": {"pano_id": "p_O1", "bearing_deg": 15, "candidate_id": "oc_2"}},
    {"x": 17.0, "y": 2.5, "floor": "f1", "radius": 0.8, "kind": "open_stair_underside",
     "note": "underside of a staircase, low headroom", "z_low_m": null, "z_high_m": null, "confidence": 0.7,
     "evidence": {"pano_id": "p_S1", "bearing_deg": 200, "candidate_id": null}}
  ]
}
```

## Merging subagent files
- `/dossier/hazards/<floor>.json`: copy `hazards` into the top-level `hazards` list, merge `node_tags` into `nodes[pano].tags` and `edge_tags` into `edges[key].tags`. Leave `rejected`, `unchecked` and `possible_doors` out of annotations.json; they go in audit.md.
- `/dossier/landmarks/<floor>.json`: append each pano's list to `nodes[pano].landmarks`; merge `node_tags`.
- De-duplicate: two hazards of the same kind within 0.5 m on the same floor are one hazard; keep the higher confidence and its evidence.

## Common mistakes
- **Edge key order** doesn't matter: `"a|b"` and `"b|a"` hit the same link. But both panos must be directly linked in the walk graph; a key for two panos that aren't neighbours does nothing. Use pano ids, not labels.
- **Units:** `x`, `y`, `radius`, `z_low_m`, `z_high_m` are metres. `radius` is a distance around the point, typically 0.4-1.0; a radius of 5 would mark a whole room.
- **Floor ids** must be the exact strings from `building_overview` (for example `"tsmq1wak12rhgn0mawksxcwcd"`), not "Floor 1". A wrong floor id means the hazard matches nothing.
- **Pano ids** must exist; labels like "22" aren't ids.
- **Hazards below 0.5 confidence** don't belong here.
- Don't put spoken-only text in tags; tags are machine words from the lists above.
- JSON only: no comments, no trailing commas, numbers not strings.
