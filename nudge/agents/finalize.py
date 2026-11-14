"""deterministic end of a dossier run. weak models write sloppy json or stop before the last write;
these make sure every run ends with valid files the router can use, and say what is missing.

merge(sid)     hazards/*.json + landmarks/*.json (+ tags) -> annotations.json, tolerant, with drop reasons
finalize(sid)  merge, keep the place catalog valid, write gaps into audit.md, fall back to a plain briefing
"""
import json
import re

from langchain_core.tools import tool

from . import tools as T

KIND_SYNONYMS = {
    "glass": "glass_wall", "glass_partition": "glass_wall", "window": "glass_wall", "glass_door": "glass_door",
    "overhang": "low_overhang", "low_ceiling": "low_overhang", "headroom": "low_overhang", "beam": "low_overhang",
    "sconce": "protruding_object", "sign": "protruding_object", "protrusion": "protruding_object",
    "protruding": "protruding_object", "shelf": "protruding_object", "fire_extinguisher": "protruding_object",
    "stair_underside": "open_stair_underside", "stairs_underside": "open_stair_underside",
    "step": "floor_level_change", "steps": "floor_level_change", "level_change": "floor_level_change",
    "ramp": "floor_level_change", "threshold": "floor_level_change", "drop": "drop_off", "dropoff": "drop_off",
    "ledge": "drop_off", "pillar": "column", "post": "column",
}
SENSE_SYNONYMS = {
    "floor": "underfoot", "surface": "underfoot", "texture": "underfoot", "feet": "underfoot", "foot": "underfoot",
    "tactile": "touch", "hand": "touch", "feel": "touch", "cane": "touch", "wall": "touch",
    "hearing": "sound", "audio": "sound", "hear": "sound", "noise": "sound", "acoustic": "sound",
    "airflow": "air", "draft": "air", "wind": "air", "temperature": "air", "odor": "smell", "scent": "smell",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as ex:
        return None, f"{p.parent.name}/{p.name} is not valid json ({ex})"


def _kind(k):
    k = re.sub(r"[^a-z_]", "_", str(k or "").strip().lower()).strip("_")
    if k in T.KINDS:
        return k
    return KIND_SYNONYMS.get(k) or next((v for s, v in KIND_SYNONYMS.items() if s in k), "other")


def _sense(s):
    s = str(s or "").strip().lower()
    return s if s in T.SENSES else SENSE_SYNONYMS.get(s) or next((v for k, v in SENSE_SYNONYMS.items() if k in s), None)


def _edge_key(g, key):
    """accept 'a|b', 'a-b', 'a,b' or [a, b], with pano ids or labels; return 'a|b' if it's a walk edge."""
    parts = key if isinstance(key, (list, tuple)) else re.split(r"\s*[|,>]\s*|\s+-\s+|(?<=\w)-(?=p_)", str(key))
    if len(parts) != 2:
        return None
    a, b = (_pano(g, x) for x in parts)
    return f"{a}|{b}" if a and b and b in g.adj.get(a, {}) else None


def _pano(g, ref):
    ref = str(ref or "").strip()
    if ref in g.nodes:
        return ref
    return next((n.id for n in g.nodes.values() if n.label == ref), None)


def merge(sid):
    """build annotations.json from the subagents' files. returns {written, merged, dropped, notes, validate}."""
    d = T.dossier_dir(sid)
    g = T.graph(sid, annotated=False)
    floors = {f["id"] for f in g.floors}
    prev, _ = T.read_annotations(sid)
    ann = {"nodes": {}, "edges": {}, "hazards": []}
    dropped, notes = [], []
    # keep what the lead already wrote by hand, then fold in the files
    sources = []
    if isinstance(prev, dict):
        sources.append(("annotations.json", prev))
    for sub in ("hazards", "landmarks"):
        for p in sorted((d / sub).glob("*.json")) if (d / sub).exists() else []:
            data, err = _load(p)
            if err:
                notes.append(err)
                continue
            sources.append((f"{sub}/{p.name}", data if isinstance(data, dict) else {sub: data, "floor": p.stem}))
    seen_h = set()
    for src, data in sources:
        file_floor = data.get("floor") if isinstance(data, dict) else None
        if file_floor not in floors:
            stem = src.split("/")[-1].removesuffix(".json")
            file_floor = stem if stem in floors else None
        for i, h in enumerate(data.get("hazards") or []):
            if not isinstance(h, dict):
                dropped.append(f"{src} hazards[{i}]: not an object")
                continue
            x, y = _num(h.get("x")), _num(h.get("y"))
            ev = h.get("evidence") if isinstance(h.get("evidence"), dict) else {}
            pano = _pano(g, ev.get("pano_id") or h.get("pano_id"))
            if (x is None or y is None) and pano:
                x, y = g.nodes[pano].x, g.nodes[pano].y  # a sweep finding: anchor at the pano it was seen from
                notes.append(f"{src} hazards[{i}]: no x/y, placed at pano {g.nodes[pano].label}")
            if x is None or y is None:
                dropped.append(f"{src} hazards[{i}]: no x/y and no evidence.pano_id")
                continue
            fl = h.get("floor") if h.get("floor") in floors else (g.nodes[pano].floor if pano else file_floor)
            if fl not in floors:
                dropped.append(f"{src} hazards[{i}]: floor {h.get('floor')!r} not in {sorted(floors)}")
                continue
            conf = _num(h.get("confidence"))
            if conf is not None and conf < 0.5:
                dropped.append(f"{src} hazards[{i}]: confidence {conf} < 0.5 (unconfirmed)")
                continue
            kind = _kind(h.get("kind"))
            key = (round(x, 1), round(y, 1), fl, kind)
            if key in seen_h:
                continue
            seen_h.add(key)
            r = _num(h.get("radius")) or 0.6
            out = {"x": round(x, 3), "y": round(y, 3), "floor": fl, "radius": min(3.0, max(0.2, r)), "kind": kind,
                   "note": str(h.get("note") or kind.replace("_", " "))[:120]}
            for k in ("z_low_m", "z_high_m", "confidence"):
                if _num(h.get(k)) is not None:
                    out[k] = _num(h.get(k))
            if pano:
                out["evidence"] = {"pano_id": pano, **{k: v for k, v in ev.items() if k != "pano_id"}}
            oe = h.get("on_edges") or ev.get("on_edges") or []
            oe = [k.split("|") for k in (_edge_key(g, e) for e in oe) if k]
            if oe:
                out["on_edges"] = oe
            ann["hazards"].append(out)
        lms = data.get("landmarks") or {}
        if isinstance(lms, list):  # [{pano_id, text, sense}] is fine too
            grouped = {}
            for lm in lms:
                if isinstance(lm, dict):
                    grouped.setdefault(lm.get("pano_id"), []).append(lm)
            lms = grouped
        nodes_in = dict(data.get("nodes") or {}) if isinstance(data.get("nodes"), dict) else {}
        for pid, info in nodes_in.items():
            if isinstance(info, dict) and info.get("landmarks"):
                lms.setdefault(pid, []).extend(info["landmarks"])
        for pid, items in lms.items():
            pano = _pano(g, pid)
            if not pano:
                dropped.append(f"{src} landmarks: {pid!r} is not a pano id")
                continue
            for lm in items if isinstance(items, list) else [items]:
                if isinstance(lm, str):
                    lm = {"text": lm}
                sense = _sense(lm.get("sense")) or "touch"
                text = str(lm.get("text") or "").strip()
                if not text:
                    dropped.append(f"{src} landmarks {pid}: empty text")
                    continue
                node = ann["nodes"].setdefault(pano, {"tags": [], "landmarks": []})
                if any(x["text"] == text for x in node["landmarks"]):
                    continue
                item = {"text": text[:140], "sense": sense}
                if _num(lm.get("bearing_deg")) is not None:
                    item["bearing_deg"] = _num(lm.get("bearing_deg"))
                node["landmarks"].append(item)
        tags = dict(data.get("node_tags") or {}) if isinstance(data.get("node_tags"), dict) else {}
        for pid, info in nodes_in.items():
            if isinstance(info, dict) and info.get("tags"):
                tags.setdefault(pid, []).extend(info["tags"])
        for pid, tl in tags.items():
            pano = _pano(g, pid)
            if pano and tl:
                node = ann["nodes"].setdefault(pano, {"tags": [], "landmarks": []})
                node["tags"] = sorted(set(node["tags"]) | {str(t) for t in (tl if isinstance(tl, list) else [tl])})
        etags = dict(data.get("edge_tags") or {})
        etags.update(data.get("edges") or {} if isinstance(data.get("edges"), dict) else {})
        for k, tl in etags.items():
            if isinstance(tl, dict):
                tl = tl.get("tags") or []
            ek = _edge_key(g, k)
            if not ek:
                dropped.append(f"{src} edge {k!r}: not a walk-graph edge")
                continue
            e = ann["edges"].setdefault(ek, {"tags": []})
            e["tags"] = sorted(set(e["tags"]) | {str(t) for t in (tl if isinstance(tl, list) else [tl])})
    ann["nodes"] = {k: {kk: vv for kk, vv in v.items() if vv} for k, v in ann["nodes"].items()}
    ann["meta"] = {"merged_by": "merge_dossier", "sources": [s for s, _ in sources]}
    (d / "annotations.json").write_text(json.dumps(ann, indent=1), encoding="utf-8")
    T._graph_cached.cache_clear()
    rep = T.validate(sid)
    return {"written": "/dossier/annotations.json", "hazards": len(ann["hazards"]),
            "landmarked_panos": sum(1 for v in ann["nodes"].values() if v.get("landmarks")),
            "tagged_edges": len(ann["edges"]), "sources": [s for s, _ in sources],
            "dropped": dropped[:30], "notes": notes[:20], "validate": rep}


def _places_ok(sid):
    from .. import places
    try:
        return places.validate(sid)
    except Exception as ex:
        return {"ok": False, "errors": [f"validate failed: {ex}"]}


def _repair_places(sid):
    """keep only catalog entries that pass on their own, so one bad entry doesn't sink the whole catalog."""
    from .. import places
    agent, _ = places.read_agent(sid)
    d = T.dossier_dir(sid)
    good, bad = [], []
    for p in (agent or {}).get("places") or []:
        p = {k: v for k, v in p.items() if k != "polygon"}
        (d / "places.json").write_text(json.dumps({"places": good + [p]}), encoding="utf-8")
        frag_dir = d / "places"
        hidden = []
        if frag_dir.exists():  # validate this entry alone
            for f in frag_dir.glob("*.json"):
                f.rename(f.with_suffix(".json.tmp"))
                hidden.append(f)
        ok = places.validate(sid).get("ok")
        for f in hidden:
            f.with_suffix(".json.tmp").rename(f)
        (good if ok else bad).append(p)
    frag_dir = d / "places"
    if frag_dir.exists():
        for f in frag_dir.glob("*.json"):
            f.rename(f.with_suffix(".json.bad"))
    (d / "places.json").write_text(json.dumps({"places": good, "meta": {"repaired_by": "finalize_dossier",
                                                                          "dropped": len(bad)}}, indent=1),
                                   encoding="utf-8")
    return good, bad


def _fallback_briefing(sid, g, ann):
    floors = [f["label"] for f in g.floors]
    has_elev = any("elevator" in n.tags for n in g.nodes.values())
    has_stairs = any("stairs" in n.tags for n in g.nodes.values())
    hz = (ann or {}).get("hazards") or []
    lines = [f"This building has {len(floors)} floor{'s' * (len(floors) != 1)}."]
    if has_elev or has_stairs:
        lines.append("Floors are connected by " + " and ".join(
            x for x, ok in (("an elevator", has_elev), ("stairs", has_stairs)) if ok) + ".")
    if hz:
        notes = sorted({h.get("note", "") for h in hz if h.get("note")})[:2]
        lines.append(f"I found {len(hz)} hazard{'s' * (len(hz) != 1)} the router will steer around"
                     + (f", including: {'; '.join(notes)}." if notes else "."))
    else:
        lines.append("No hazards were confirmed yet, so keep using your cane as usual.")
    lines.append("This briefing was written automatically because the survey did not finish; "
                 "entrances and landmarks were not fully checked.")
    return " ".join(lines) + "\n"


def finalize(sid, reason="end of run"):
    """always leaves valid annotations.json and place catalog, an audit.md with gaps, and a briefing."""
    d = T.dossier_dir(sid)
    out = {"reason": reason}
    m = merge(sid)
    if not m["validate"].get("ok"):
        # last resort: an empty but valid file beats a broken one
        (d / "annotations.json").write_text(json.dumps({"nodes": {}, "edges": {}, "hazards": [],
                                                         "meta": {"emptied_by": "finalize_dossier"}}), encoding="utf-8")
        T._graph_cached.cache_clear()
        m["emptied"] = m["validate"].get("errors")
    out["annotations"] = {k: m[k] for k in ("hazards", "landmarked_panos", "tagged_edges", "dropped")}
    pv = _places_ok(sid)
    if not pv.get("ok"):
        from .. import places
        if places.read_agent(sid)[0] is None:
            (d / "places.json").write_text(json.dumps({"places": [], "meta": {
                "note": "seed only; place-indexer did not write a catalog"}}), encoding="utf-8")
        else:
            good, bad = _repair_places(sid)
            out["places_dropped"] = len(bad)
        pv = _places_ok(sid)
    out["places"] = {k: pv.get(k) for k in ("ok", "places", "named_by_agent", "features", "still_unnamed")}
    g = T.graph(sid, annotated=False)
    gaps = []
    for f in g.floors:
        if not (d / "hazards" / f"{f['id']}.json").exists():
            gaps.append(f"floor {f['id']}: no hazard survey file")
        if not (d / "landmarks" / f"{f['id']}.json").exists():
            gaps.append(f"floor {f['id']}: no landmark file")
    try:
        a = T.audit(sid, "blind")
        missing = a["turn_points_without_landmarks"]
        if missing:
            gaps.append(f"{len(missing)} turn points without a non-visual landmark: "
                        + ", ".join(p["label"] for p in missing[:15]))
        for u in a["unreachable"]:
            gaps.append(f"unreachable: {u['room']}")
    except Exception as ex:
        gaps.append(f"audit failed: {ex}")
    if pv.get("still_unnamed"):
        gaps.append(f"unnamed places: {', '.join(pv['still_unnamed'][:10])}")
    if not (d / "site.md").exists():
        gaps.append("no site survey (/dossier/site.md)")
    for e in m["dropped"]:
        gaps.append(f"dropped while merging: {e}")
    audit_p = d / "audit.md"
    prev = audit_p.read_text(encoding="utf-8") if audit_p.exists() else "# Audit\n\n(the lead did not write an audit)\n"
    prev = prev.split("\n## Gaps (written automatically)")[0].rstrip()
    audit_p.write_text(prev + "\n\n## Gaps (written automatically)\n\n" + f"Reason: {reason}.\n\n"
                       + ("\n".join(f"- {x}" for x in gaps) if gaps else "- none") + "\n", encoding="utf-8")
    out["gaps"] = gaps
    if not (d / "briefing.md").exists():
        ann, _ = T.read_annotations(sid)
        (d / "briefing.md").write_text(_fallback_briefing(sid, g, ann), encoding="utf-8")
        out["briefing"] = "fallback"
    return out


@tool(parse_docstring=True)
def merge_dossier(sid: str) -> str:
    """Merge every /dossier/hazards/<floor>.json and /dossier/landmarks/<floor>.json (plus their node_tags and
    edge_tags) into /dossier/annotations.json, fixing small mistakes (string numbers, synonyms for kinds and
    senses, pano labels instead of ids) and dropping what can't be fixed, with the reason. Use this instead of
    writing annotations.json by hand. It runs validate_annotations at the end and returns its report.

    Args:
        sid: Matterport model id.
    """
    return T._j(merge(sid))


@tool(parse_docstring=True)
def finalize_dossier(sid: str) -> str:
    """Last step of every run, also safe to call when you are out of budget: merges annotations, keeps the
    place catalog valid (drops entries that fail), appends what is still missing to /dossier/audit.md, and writes
    a plain /dossier/briefing.md if you haven't. Call it after writing your audit.md and briefing.md.

    Args:
        sid: Matterport model id.
    """
    return T._j(finalize(sid, "finalize_dossier called by the lead"))
