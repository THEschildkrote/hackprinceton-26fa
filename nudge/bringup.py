"""one command from "keys pasted" to "real demo working".

run: python -m nudge.bringup [--sid SID] [--steps all|keys,probe,pick,fetch,calibrate,georef,costar,dossier]
                             [--goal "Conference Room"] [--dossier] [--refresh]

each step prints [pass] / [fail] / [skip] with the reason, and the report lands in cache/bringup/<sid>.json.
key values are never printed: only whether a key is present.
"""
import argparse
import io
import json
import math
import re
import sys
import time

from .mp import ROOT, secret

OUT = ROOT / "cache" / "bringup"
STEPS = ["keys", "probe", "pick", "fetch", "calibrate", "georef", "costar", "dossier"]
SAMPLE = "zvKHR9DHNjJ"  # the quickstart's example model, used to probe the schema before a sid is picked

KEYS = {
    "matterport": ["MATTERPORT_API_KEY", "MATTERPORT_API_TOKEN"],
    "matterport sdk (3d tour)": ["MATTERPORT_SDK_KEY"],
    "vexcel (outdoor, aerial)": ["VEXCEL_API_KEY"],
    "costar kit": ["COSTAR_ZIP_PASSWORD", "COSTAR_KIT_URL|COSTAR_KIT_ZIP"],
    "model for the agents": ["OPENROUTER_API_KEY|ANTHROPIC_API_KEY"],
    "photon (imessage)": ["PHOTON_API_KEY", "PHOTON_PROJECT_ID"],
}
DOORISH = re.compile(r"door|open|entr|arch|pass|gap|portal|threshold", re.I)


def _have(name):
    return any(bool(secret(n, required=False)) for n in name.split("|"))


class Ctx:
    def __init__(self, sid=None, goal=None, dossier=False, refresh=False, echo=print):
        self.sid, self.goal, self.run_dossier, self.refresh, self.echo = sid, goal, dossier, refresh, echo
        self.sid_given = sid is not None
        self.report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": {}}
        self.model = self.graph = None

    def save(self):
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{self.sid or 'unpicked'}.json"
        path.write_text(json.dumps(self.report, indent=1, default=str), encoding="utf-8")
        return path


def _synthetic(ctx):
    return bool(ctx.sid) and ctx.sid.startswith("synthetic")


# steps: each returns (status, message, data)

def step_keys(ctx):
    have = {group: [n for n in names if _have(n)] for group, names in KEYS.items()}
    missing = {group: [n for n in names if not _have(n)] for group, names in KEYS.items()}
    lines = [f"{g}: {'ok' if not missing[g] else 'missing ' + ', '.join(missing[g])}" for g in KEYS]
    flat = [n.split("|")[0] for g in missing.values() for n in g]
    data = {"present": have, "missing": missing}
    if flat:
        data["fix"] = "python -m nudge.setkey " + " ".join(flat)
    if _synthetic(ctx):
        return "pass", "synthetic needs no keys. " + "; ".join(lines), data
    if missing["matterport"]:
        return "fail", "Matterport API key/token missing; run: " + data["fix"], data
    return "pass", "; ".join(lines) + (f". to add the rest: {data['fix']}" if flat else ""), data


def _types(gql, sid):
    d = gql("query($id: ID!) { model(id: $id) { floors { edges { type } } } }", id=sid)["model"]
    out = {}
    for f in d.get("floors") or []:
        for e in f.get("edges") or []:
            t = (e.get("type") or "wall").lower()
            out[t] = out.get(t, 0) + 1
    return out


def passable_suggestion(edge_types):
    from .walkgraph import PASSABLE
    return sorted(t for t in edge_types if t != "wall" and t not in PASSABLE and DOORISH.search(t))


def step_probe(ctx):
    if _synthetic(ctx):
        return "skip", "synthetic building: no Model API to probe", {}
    from .mp import MatterportError, graph as gql
    try:
        root = gql("{ rootFolder { name modelCountSummary(includeModelsInSubfolders: true) } }")["rootFolder"]
    except (MatterportError, SystemExit) as ex:
        return "fail", f"Model API rejected the key ({ex}); re-enter with python -m nudge.setkey " \
                       f"MATTERPORT_API_KEY MATTERPORT_API_TOKEN", {}
    data = {"folder": root.get("name"), "model_count": root.get("modelCountSummary")}

    def fields(tn):
        t = gql("query($n: String!) { __type(name: $n) { fields { name } } }", n=tn).get("__type")
        return [f["name"] for f in (t or {}).get("fields") or []]

    loc = {}
    for tn in ("Location", "PanoLocation", "Sweep"):
        try:
            fs = fields(tn)
        except MatterportError:
            continue
        if fs:
            loc[tn] = fs
    lf = sorted({f for fs in loc.values() for f in fs})
    data["location_fields"] = loc
    data["pano_floor"] = "floor" in lf
    data["pano_room"] = "room" in lf
    data["rotation_field"] = next((f for f in ("rotation", "orientation", "quaternion", "heading") if f in lf), None)
    data["neighbors_field"] = next((f for f in ("neighbors", "neighbours", "adjacent") if f in lf), None)
    try:
        data["edge_types"] = _types(gql, ctx.sid or SAMPLE)
    except MatterportError as ex:
        data["edge_types_error"] = str(ex)
        data["edge_types"] = {}
    data["passable_suggestion"] = passable_suggestion(data["edge_types"])
    msg = (f"{data['model_count']} models; pano floor field: {data['pano_floor']}, room: {data['pano_room']}, "
           f"rotation: {data['rotation_field']}; edge types {data['edge_types']}")
    if data["passable_suggestion"]:
        msg += f"; add to walkgraph.PASSABLE: {data['passable_suggestion']}"
    if not data["rotation_field"]:
        msg += "; no per-pano rotation field: calibrate FACE1_YAW_DEG per building"
    return "pass", msg, data


def rank_models(limit=5):
    from .pick import load, score
    rows = [r for r in load() if r["segment"] == "commercial" and r["costar_property_id"] and r["lat"]]
    rows.sort(key=lambda r: (r["splat"] == "yes", score(r)), reverse=True)
    return [{"sid": r["sid"], "name": r["name"], "city": r["city"], "splat": r["splat"],
             "costar_property_id": r["costar_property_id"], "panos": r["pano_count"], "floors": r["floor_count"],
             "score": score(r)} for r in rows[:limit]]


def step_pick(ctx):
    if ctx.sid_given:
        return "pass", f"using --sid {ctx.sid}", {"sid": ctx.sid}
    top = rank_models()
    if not top:
        return "fail", "no commercial model with a CoStar id and lat/lon in models_index.csv", {}
    ctx.sid = top[0]["sid"]
    t = top[0]
    return "pass", (f"picked {t['sid']} ({t['name']}, {t['city']}): CoStar {t['costar_property_id']}, "
                    f"splat {t['splat']}, {t['panos']} panos, {t['floors']} floors"), {"sid": ctx.sid, "top": top}


def graph_stats(g):
    comp = g.components()
    sizes = {}
    for c in comp.values():
        sizes[c] = sizes.get(c, 0) + 1
    main = max(sizes, key=sizes.get) if sizes else None
    stray = [g.nodes[n].label for n, c in comp.items() if c != main]
    tags = {}
    for e in g.edges():
        for t in e.tags:
            tags[t] = tags.get(t, 0) + 1
    floors = {f["id"]: sum(1 for n in g.nodes.values() if n.floor == f["id"]) for f in g.floors}
    return {"nodes": len(g.nodes), "edges": sum(1 for _ in g.edges()), "components": len(sizes),
            "unconnected_panos": stray, "edge_tags": tags, "panos_per_floor": floors, "warnings": g.warnings}


def step_fetch(ctx):
    from . import fetch, walkgraph
    try:
        ctx.model = fetch.load(ctx.sid, refresh=ctx.refresh)
    except Exception as ex:
        return "fail", f"could not load {ctx.sid}: {ex}", {}
    m = ctx.model
    panos = m.get("panoLocations") or []
    if not panos:
        return "fail", f"{ctx.sid} has no panos the key can see", {}
    edge_types = {}
    for f in m.get("floors") or []:
        for e in f.get("edges") or []:
            t = (e.get("type") or "wall").lower()
            edge_types[t] = edge_types.get(t, 0) + 1
    ctx.graph = walkgraph.build(m)
    st = graph_stats(ctx.graph)
    data = {"name": m.get("name"), "floors": len(m.get("floors") or []), "rooms": len(m.get("rooms") or []),
            "panos": len(panos), "dropped": m.get("_dropped") or [], "edge_types": edge_types, **st}
    msg = (f"{data['name']}: {data['floors']} floors, {data['rooms']} rooms, {data['panos']} panos; graph "
           f"{st['nodes']} nodes, {st['edges']} edges, {st['components']} component(s)")
    if data["dropped"]:
        msg += f"; fields the API doesn't have: {data['dropped']}"
    sugg = passable_suggestion(edge_types)
    if st["unconnected_panos"] or any("bridged" in w for w in st["warnings"]):
        data["fix"] = (f"panos are cut off ({len(st['unconnected_panos'])} unconnected). door edges may not be typed "
                       f"as walkgraph.PASSABLE expects; edge types here: {edge_types}")
        if sugg:
            data["fix"] += f". add {sugg} to PASSABLE in nudge/walkgraph.py"
        msg += ". " + data["fix"]
    return "pass", msg, data


def _corridor_pano(g):
    """a pano whose four horizon distances differ a lot: corridors make the calibration obvious."""
    from .perceive import wall_distance
    named = [n for n in g.nodes.values() if n.room_label and re.search(r"hall|corridor|lobby", n.room_label, re.I)]
    pool = named or list(g.nodes.values())
    best = None
    for n in pool[:60]:
        ds = [wall_distance(g, n, y) for y in (0, 90, 180, 270)]
        ds = [d for d in ds if d is not None]
        if len(ds) < 4:
            continue
        spread = max(ds) / max(min(ds), 0.3)
        if best is None or spread > best[0]:
            best = (spread, n)
    return (best[1] if best else pool[0])


def openness(jpeg):
    """how much structure a view has: far views show more edges than a wall in your face."""
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(jpeg)).convert("L")
    w, h = im.size
    im = im.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)).filter(ImageFilter.FIND_EDGES)
    data = im.tobytes()
    return sum(data) / len(data)


def suggest_face1(views):
    """which yaw offset makes 'more structure' line up with 'farther wall'. None when it's not clear."""
    dist = [math.log(max(v["expected_wall_m"] or 0.3, 0.3)) for v in views]
    opn = [openness(v["jpeg"]) for v in views]

    def corr(a, b):
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) or 1e-9
        return num / den
    # the view rendered at yaw i actually shows yaw i+k if face1 is off by k*90
    scores = {k * 90: corr(opn, dist[k:] + dist[:k]) for k in range(4)}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, runner = ranked[0], ranked[1]
    sure = best[1] > 0.6 and best[1] - runner[1] > 0.3
    return (best[0] if sure else None), {str(k): round(v, 2) for k, v in scores.items()}


def step_calibrate(ctx):
    from . import perceive
    if ctx.graph is None:
        return "skip", "needs the fetch step first", {}
    n = _corridor_pano(ctx.graph)
    try:
        cal = perceive.calibrate(ctx.sid, n.id)
    except Exception as ex:
        return "fail", f"could not render pano {n.label}: {ex}", {}
    OUT.mkdir(parents=True, exist_ok=True)
    files = []
    for v in cal["views"]:
        p = OUT / f"{ctx.sid}_calib_{n.id[:12]}_{v['yaw_deg']}.jpg"
        p.write_bytes(v["jpeg"])
        files.append(str(p))
    shift, scores = suggest_face1(cal["views"])
    exp = {v["yaw_deg"]: None if v["expected_wall_m"] is None else round(v["expected_wall_m"], 1) for v in cal["views"]}
    data = {"pano_id": n.id, "pano_label": n.label, "room": n.room_label, "face1_yaw_deg": cal["face1_yaw_deg"],
            "rotation_deg": cal.get("rotation_deg"), "expected_wall_m": exp, "views": files,
            "offset_scores": scores, "suggested_offset_deg": shift}
    msg = f"pano {n.label} ({n.room_label}); expected wall distance by yaw {exp}; views in {OUT}"
    if shift is None:
        msg += ". offset unclear: compare the 4 views with the distances by eye and set FACE1_YAW_DEG in nudge/perceive.py"
    elif shift == 0:
        msg += f". FACE1_YAW_DEG={cal['face1_yaw_deg']} looks right"
    else:
        # the image heuristic was wrong on the first real building (it said 270; 0 was right), so only hint
        msg += (f". image heuristic hints a {shift}° offset, but it is unreliable on real panos: check by eye that "
                f"the yaw-0 view faces the longest expected wall distance before changing FACE1_YAW_DEG")
    return "pass", msg, data


def overlay(jpeg, meta, site, path):
    """the ortho with the scan outline (as registered) and the entrance candidates drawn on it."""
    from PIL import Image, ImageDraw
    from .georef import outline
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = im.size
    gsd = meta["gsd_m"]
    dr = ImageDraw.Draw(im)

    def px(e, n):
        return (w / 2 + e / gsd, h / 2 - n / gsd)
    ring = [site.geo.model_to_enu(x, y) for x, y in outline(site.gi)]
    if ring:
        dr.line([px(*p) for p in ring + ring[:1]], fill=(255, 200, 0), width=3)
    for i, p in enumerate(site.portals):
        node = site.gi.nodes.get(p["pano_id"])
        if node is None:
            continue
        x, y = px(*site.geo.model_to_enu(node.x, node.y))
        dr.ellipse([x - 7, y - 7, x + 7, y + 7], outline=(0, 255, 255), width=3)
        dr.text((x + 9, y - 6), str(i), fill=(0, 255, 255))
    im.save(path, quality=88)
    return str(path)


def step_georef(ctx):
    from . import handoff, perceive
    try:
        site = handoff.load_site(ctx.sid, refresh=ctx.refresh)
    except Exception as ex:
        hint = " (needs VEXCEL_API_KEY: python -m nudge.setkey VEXCEL_API_KEY)" if not _have("VEXCEL_API_KEY") else ""
        return "fail", f"could not build the outdoor site: {ex}{hint}", {}
    geo = site.geo.to_dict()
    data = {"georef": geo, "entrance_candidates": len(site.portals), "outdoor_nodes": len(site.og.nodes),
            "warnings": site.warnings, "portals": [{k: p.get(k) for k in ("pano_id", "outdoor_node", "width_m", "step",
                                                                      "confirmed")} for p in site.portals[:6]]}
    try:
        jpeg, meta = perceive.aerial(ctx.sid)
        OUT.mkdir(parents=True, exist_ok=True)
        data["overlay"] = overlay(jpeg, meta, site, OUT / f"{ctx.sid}_georef.jpg")
    except Exception as ex:
        data["overlay_error"] = str(ex)
    msg = (f"scan on map: rotation {geo.get('theta_deg')}°, rmse {geo.get('rmse_m')} m, "
           f"{'ambiguous' if geo.get('ambiguous') else 'unambiguous'}; {len(site.portals)} entrance candidate(s); "
           f"outdoor graph {len(site.og.nodes)} nodes")
    if not geo.get("ok"):
        msg += "; fit is poor, entrance positions are rough"
    if not site.portals:
        return "fail", msg + "; no entrance candidates, so door-to-door can't route", data
    return "pass", msg, data


def step_costar(ctx):
    from . import costar
    r = costar.lookup(ctx.sid)
    if r.get("error"):
        status = "skip" if "no CoStar property id" in r["error"] else "fail"
        return status, f"CoStar: {r['error']}", r
    f = costar.accessibility_facts(r["record"])
    shown = {k: v for k, v in f["facts"].items() if not isinstance(v, (list, dict))}
    data = {**{k: r[k] for k in ("key", "kind", "file")}, **f}
    try:
        data["doc_fields"] = costar.field_hints()[:80]
    except Exception:
        pass
    return "pass", f"CoStar {r['kind']} record {r['key']}: {shown}; missing {f['missing']}", data


def _goal(ctx):
    if ctx.goal:
        return ctx.goal
    from .agents.tools import key_places
    from .router import NoRoute, plan
    entry, places = key_places(ctx.graph)
    best = None
    for _, (label, node) in places.items():
        try:
            r = plan(ctx.graph, entry.id, node.id, "blind")
        except NoRoute:
            continue
        if best is None or r.length > best[0]:
            best = (r.length, label)
    return best[1] if best else None


def step_dossier(ctx):
    from .agents import llm
    if not ctx.run_dossier:
        return "skip", "pass --dossier to start the route-scoped quick survey (runs the deep agent)", {}
    if not (llm.has_credentials(llm.spec()) and llm.has_credentials(llm.spec("vision"))):
        return "skip", "no model credentials (OPENROUTER_API_KEY or ANTHROPIC_API_KEY)", {}
    if ctx.graph is None:
        return "skip", "needs the fetch step first", {}
    from .agents import dossier
    from .agents.tools import route_focus
    goal = _goal(ctx)
    if not goal:
        return "fail", "no reachable room to survey a route to; pass --goal", {}
    rf = route_focus(ctx.sid, goal, "blind")
    t = time.time()
    out = dossier.run(ctx.sid, focus=rf["focus"], goal=goal, on_event=lambda e: ctx.echo(f"    {e}"))
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    data = {"goal": goal, "focus": rf["focus"], "route_m": rf["length_m"], "seconds": round(time.time() - t), "files": files}
    ok = "annotations.json" in files
    return ("pass" if ok else "fail"), (f"quick survey to {goal} ({rf['length_m']} m, {len(rf['focus'])} panos) wrote "
                                        f"{files}" + ("" if ok else "; no annotations.json")), data


RUNNERS = {"keys": step_keys, "probe": step_probe, "pick": step_pick, "fetch": step_fetch,
           "calibrate": step_calibrate, "georef": step_georef, "costar": step_costar, "dossier": step_dossier}


def next_commands(sid):
    return [
        "python -m uvicorn nudge.server:app --port 8765",
        f"http://localhost:8765/tour.html?sid={sid}",
        f"http://localhost:8765/dossier.html?sid={sid}",
        "python -m nudge.photon_bot",
    ]


def run(sid=None, steps=None, goal=None, dossier=False, refresh=False, echo=print):
    ctx = Ctx(sid, goal, dossier, refresh, echo)
    steps = STEPS if not steps or steps == ["all"] else steps
    failed = False
    for name in steps:
        if name not in RUNNERS:
            echo(f"[fail] {name}: unknown step (choose from {', '.join(STEPS)})")
            failed = True
            continue
        if name not in ("keys", "probe", "pick") and not ctx.sid:
            status, msg, data = "skip", "no building picked yet (run pick or pass --sid)", {}
        else:
            try:
                status, msg, data = RUNNERS[name](ctx)
            except Exception as ex:  # a step must never take the whole bring-up down
                status, msg, data = "fail", f"{type(ex).__name__}: {ex}", {}
        ctx.report["steps"][name] = {"status": status, "message": msg, "data": data}
        ctx.report["sid"] = ctx.sid
        echo(f"[{status}] {name}: {msg}")
        failed |= status == "fail"
        ctx.save()
    if ctx.sid:
        ctx.report["next"] = next_commands(ctx.sid)
        echo("\nnext:")
        for c in ctx.report["next"]:
            echo(f"  {c}")
    path = ctx.save()
    echo(f"\nreport: {path}")
    return ctx.report, (1 if failed else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sid")
    ap.add_argument("--steps", default="all", help="all, or a comma list of " + ",".join(STEPS))
    ap.add_argument("--goal", help="destination for the quick survey (default: the farthest room)")
    ap.add_argument("--dossier", action="store_true", help="also run the route-scoped quick survey")
    ap.add_argument("--refresh", action="store_true", help="refetch instead of using caches")
    a = ap.parse_args(argv)
    _, code = run(a.sid, [s.strip() for s in a.steps.split(",") if s.strip()], a.goal, a.dossier, a.refresh)
    return code


if __name__ == "__main__":
    sys.exit(main())
