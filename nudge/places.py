"""the place catalog: what each section of a building (and its outside) is, and where.

agents decide WHAT a place is and what people call it (names, aliases, serves, descriptions, features).
geometry only ever comes from the data: room polygons and panos from the walk graph, entrances from
handoff/georef, outdoor nodes from the outdoor graph. features are anchored to a pano + bearing.

  seed(sid)        draft catalog from the data
  load(sid)        agent's /dossier/places.json merged over the seed (agent wins on words, seed on geometry)
  resolve(src, q)  "how do I get to the kitchenette" -> ranked places, or an explicit question when ambiguous
  goal_nodes(p)    node ids the router should aim for
  where_am_i(...)  the place a position is in, with its description
"""
import difflib
import json
import re

from .geom import dist, point_in_poly

KINDS = {"room", "zone", "feature", "tenant", "entrance", "outdoor_zone", "floor"}
SOURCES = {"matterport", "agent", "costar", "costar+guess", "vexcel", "osm"}
GENERIC = {"room", "area", "space", "place", "the", "a", "an", "my", "of", "to", "on", "in", "at", "floor"}

# what a room label suggests it is for. the agent refines these per building.
SERVES_BY_LABEL = [
    (("kitchen", "kitchenette", "pantry", "break"), ["water", "food", "sink"]),
    (("bath", "restroom", "toilet", "wc", "lavatory", "washroom"), ["restroom", "water"]),
    (("lobby", "reception", "foyer"), ["help_desk", "exit", "seating"]),
    (("entr", "vestibule"), ["exit"]),
    (("elevator", "lift"), ["elevator"]),
    (("stair",), ["stairs"]),
    (("conference", "meeting", "board"), ["meeting", "seating"]),
    (("dining", "cafe", "cafeteria"), ["food", "seating"]),
    (("living", "lounge", "waiting"), ["seating"]),
    (("office", "suite"), ["work"]),
    (("hall", "corridor"), ["corridor"]),
    (("laundry",), ["laundry"]),
    (("gym", "fitness"), ["gym"]),
]

# words people use for a need -> the serves tag
INTENTS = {
    "water": ["water", "drink", "thirsty", "fountain", "sink", "tap", "refill"],
    "restroom": ["restroom", "bathroom", "toilet", "wc", "washroom", "loo", "lavatory", "men's room",
                 "ladies room", "accessible toilet", "ladies", "men's", "mens", "women's", "womens", "gents",
                 "powder room", "half bath"],
    "exit": ["exit", "way out", "leave", "get out", "outside", "entrance", "front door", "main door"],
    "help_desk": ["help", "reception", "front desk", "information", "info desk", "staff", "someone to ask",
                  "security desk", "concierge"],
    "food": ["food", "eat", "snack", "coffee", "cafe", "vending", "lunch", "microwave"],
    "seating": ["sit", "seat", "bench", "chair", "sit down", "somewhere to rest"],
    "elevator": ["elevator", "lift"],
    "stairs": ["stairs", "stair", "staircase", "stairwell", "steps"],
    "meeting": ["meeting", "conference"],
}
SERVES = set(INTENTS) | {"sink", "work", "corridor", "laundry", "gym", "parking", "drop_off", "crossing",
                         "mail", "printer", "rest"}

LEADING = [
    r"how (?:do|can|would|should) i (?:get|go|walk|make it) to", r"how to get to", r"how to go to",
    r"(?:show me |tell me )?the way to", r"way to", r"directions to", r"where(?:'s| is| are)",
    r"where can i (?:get|find|go)", r"is there (?:a|an|any)", r"take me to", r"get me to", r"bring me to",
    r"guide me to", r"navigate to", r"go to", r"i (?:need|want|would like|'d like) (?:to go to|to get to|to find|to)?",
    r"(?:can you )?find(?: me)?", r"somewhere (?:i can|to)", r"a place (?:i can|to)", r"place to",
]
LEAD_RE = re.compile(r"^(?:please\s+)?(?:" + "|".join(LEADING) + r")\s+", re.I)


# ---------- seed: what the data already tells us ----------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def _serves(label):
    low = (label or "").lower()
    out = []
    for words, tags in SERVES_BY_LABEL:
        if any(w in low for w in words):
            out += [t for t in tags if t not in out]
    return out


def _area(poly):
    return abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1] for i in range(len(poly)))) / 2


def _centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _map_of(node_id, default):
    return node_id.split(":", 1)[0] if ":" in node_id else default


def _floor_label(g, fid):
    f = next((f for f in g.floors if f["id"] == fid), None)
    return f["label"] if f else fid


def _entry_panos(g, ids):
    """panos of a room you step into it from: the ones with a link to another room (doors, openings)."""
    ids = set(ids)
    entry = [n for n in ids if any(v not in ids and "vertical" not in e.tags
                                   for v, e in g.adj[n].items())]
    return sorted(entry) or sorted(ids)


def seed_from_graph(g, sid=None, costar=None, site=None):
    sid = sid or getattr(g, "sid", None) or "?"
    places = []
    by_room = {}
    for n in g.nodes.values():
        if n.room:
            by_room.setdefault(n.room, []).append(n.id)
    # labelled rooms first: used to disambiguate duplicates and name unnamed spaces
    centres = {}
    for rid, r in g.rooms.items():
        ids = by_room.get(rid, [])
        pts = r["poly"] if r.get("poly") and len(r["poly"]) >= 3 else [g.nodes[i].xy for i in ids]
        if pts:
            centres[rid] = _centroid(pts)
    labels = {}
    for rid, r in g.rooms.items():
        lab = r.get("label")
        if lab and lab != "Void" and (by_room.get(rid) or r.get("poly")):
            labels.setdefault(lab, []).append(rid)

    def near_label(rid):
        c = centres.get(rid)
        if not c:
            return None
        best = None
        for other, oc in centres.items():
            lab = g.rooms[other].get("label")
            # only landmarks a visitor would recognise: skip generic labels like "Other"
            if other == rid or not lab or lab in ("Void", "Other", "Unknown") or len(labels.get(lab, [])) != 1:
                continue
            if g.rooms[other]["floor"] != g.rooms[rid]["floor"]:
                continue
            d = dist(c, oc)
            if best is None or d < best[0]:
                best = (d, lab)
        return best[1].lower() if best else None

    unnamed = 0
    for rid, r in g.rooms.items():
        ids = sorted(by_room.get(rid, []))
        if not ids and not r.get("poly"):
            continue
        lab = r.get("label")
        fid = r["floor"]
        name, conf, aliases = lab, 0.8, []
        if not lab or lab == "Void":
            if not ids:
                continue  # an empty void is not somewhere anyone goes
            unnamed += 1
            near = near_label(rid)
            name = f"unnamed space {unnamed}" + (f", by the {near}" if near else "")
            conf = 0.1
        elif len(labels.get(lab, [])) > 1:
            same_floor = [x for x in labels[lab] if g.rooms[x]["floor"] == fid]
            aliases = [lab]
            if len(same_floor) == 1:
                name = f"{lab}, {_floor_label(g, fid)}"
            else:
                # number per floor and say the floor; "by the <neighbour>" was mostly "by the entrance" on real scans
                k = same_floor.index(rid) + 1
                multi = len({g.rooms[x]["floor"] for x in labels[lab]}) > 1
                name = f"{lab} {k}, {_floor_label(g, fid)}" if multi else f"{lab} {k}"
            conf = 0.7
        places.append({
            "id": f"room:{rid}", "name": name, "kind": "room", "aliases": aliases, "serves": _serves(lab),
            "description": "", "map": _map_of(ids[0], sid) if ids else sid, "floor": fid, "rooms": [rid],
            "panos": ids, "entry_panos": _entry_panos(g, ids) if ids else [],
            "polygon": [list(p) for p in r["poly"]] if r.get("poly") else None,
            "source": "matterport", "confidence": conf, "unnamed": not lab or lab == "Void",
            "matterport_label": lab})
    for f in g.floors:
        ids = sorted(n.id for n in g.nodes.values() if n.floor == f["id"])
        if not ids:
            continue
        conn = [i for i in ids if g.nodes[i].tags & {"elevator", "stairs"}] or ids
        places.append({"id": f"floor:{f['id']}", "name": f["label"], "kind": "floor", "aliases": [],
                       "serves": [], "description": "", "map": _map_of(ids[0], sid), "floor": f["id"],
                       "rooms": [], "panos": ids, "entry_panos": sorted(conn), "polygon": None,
                       "source": "matterport", "confidence": 0.9})
    places += _tenants(places, costar, g)
    places += _entrances(g, sid, site)
    return {"places": places, "meta": {"sid": sid, "seeded": True, "rooms": len(g.rooms), "unnamed": unnamed}}


FLOOR_WORDS = {"basement": 0, "lowerlevel": 0, "groundfloor": 1, "firstfloor": 1, "secondfloor": 2,
               "thirdfloor": 3, "fourthfloor": 4, "fifthfloor": 5}
# rooms that can't be someone's office or suite
NOT_SUITE = {"hallway", "bathroom", "closet", "laundry", "kitchen", "staircase", "stairs", "elevator", "void", "other",
             "entrance", "utility", "garage"}
SUITE_WORDS = ("office", "suite", "reception", "lobby", "conference")


def _costar_tenants(costar):
    """tenants from whichever shape the costar record came in (top level, _related, or facts)."""
    c = costar or {}
    return c.get("tenants") or (c.get("_related") or {}).get("tenants") or (c.get("facts") or {}).get("tenants") or []


def _tenant_room(places, g, floor_word):
    """the room a tenant most likely occupies on its costar floor: a suite-like label first, else the biggest
    room people work in. returns (room place, guessed)."""
    n = FLOOR_WORDS.get(re.sub(r"[^a-z]", "", str(floor_word or "").lower()))
    if n is None or g is None or not g.floors:
        return None, True
    floors = [f["id"] for f in g.floors]
    fid = floors[min(max(n - 1, 0), len(floors) - 1)] if n else floors[0]
    rooms = [p for p in places if p["kind"] == "room" and p.get("floor") == fid and p.get("panos")]
    suite = [p for p in rooms if any(w in (p.get("matterport_label") or p["name"]).lower() for w in SUITE_WORDS)]
    if suite:
        return max(suite, key=lambda p: _area(p["polygon"]) if p.get("polygon") else 0), len(suite) > 1
    work = [p for p in rooms if (p.get("matterport_label") or p["name"].split(",")[0].split(" ")[0]).lower()
            not in NOT_SUITE]
    if not work:
        return None, True
    return max(work, key=lambda p: _area(p["polygon"]) if p.get("polygon") else 0), True


def plural(phrase):
    """pluralise the head noun: "place to get water" -> "places to get water", "bathroom" -> "bathrooms"."""
    words = phrase.split()
    i = next((k for k, w in enumerate(words) if w in ("to", "for", "with", "of")), len(words)) - 1
    if i < 0:
        return phrase
    w = words[i]
    words[i] = w[:-1] + "ies" if w.endswith("y") and w[-2:-1] not in "aeiou" else \
        w + "es" if w.endswith(("s", "x", "ch", "sh")) else w + "s"
    return " ".join(words)


def the(place):
    """a place name as it's said in a sentence: "the kitchen", but "Hallway 2, Floor 1" and "DJF Insurance"."""
    name = place.get("name") if isinstance(place, dict) else str(place)
    kind = place.get("kind") if isinstance(place, dict) else None
    head = name.split(",")[0]
    if kind == "tenant" or re.search(r"\d", head) or name[:1].isdigit():
        return name
    return f"the {name}"


def _tenant_aliases(name):
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in ("llc", "inc", "co", "corp", "the", "of", "de")]
    al = set()
    for kind in ("insurance", "dental", "law", "medical", "clinic", "bank", "realty", "accounting", "consulting"):
        if kind in words:
            al |= {kind, f"{kind} office", f"the {kind} office", f"{kind} agency" if kind == "insurance" else kind}
    if words:
        al.add(words[0])  # "djf"
    return sorted(al - {name.lower()})


def _tenants(places, costar, g=None):
    """costar tenants as destinations: matched by suite name, else placed on their costar floor (a guess that says
    so), else kept with an unknown location."""
    out = []
    for t in _costar_tenants(costar):
        name = t.get("name") or t.get("tenant") or t.get("tenantName")
        if not name:
            continue
        suite = str(t.get("suite") or t.get("suiteName") or t.get("location") or "").strip()
        room = next((p for p in places if p["kind"] == "room" and suite and
                     (p.get("matterport_label") or "").lower() == suite.lower()), None) if suite else None
        source, conf = "costar", 0.7
        if room is None:
            room, guessed = _tenant_room(places, g, t.get("floor"))
            source, conf = ("costar+guess", 0.6) if room else ("costar", 0.3)
        floor_txt = _floor_label(g, room["floor"]) if room and g is not None else None
        desc = f"{name}" + (f", in {suite}" if suite else "") + (f", on {floor_txt}" if floor_txt else "")
        out.append({"id": f"tenant:{_slug(name)}", "name": name, "kind": "tenant",
                    "aliases": ([suite] if suite else []) + _tenant_aliases(name),
                    "serves": [], "description": desc,
                    "map": room["map"] if room else None, "floor": room["floor"] if room else None,
                    "rooms": room["rooms"] if room else [], "panos": room["panos"] if room else [],
                    "entry_panos": room["entry_panos"] if room else [], "polygon": None, "source": source,
                    "confidence": conf if room else 0.3, "location_known": bool(room),
                    "in_room": room["name"] if room else None})
    return out


def _entrances(g, sid, site):
    """entrances and outdoor zones from a loaded handoff site (needs vexcel keys, so only when already loaded)."""
    if site is None:
        try:
            from . import handoff
            site = handoff._SITES.get(sid)
        except Exception:
            site = None
    if site is None:
        return []
    out = []
    for i, p in enumerate(site.portals or []):
        pano = p.get("pano_id")
        if pano not in g.nodes:
            continue
        name = "main entrance" if i == 0 else f"entrance {i + 1}"
        out.append({"id": f"entrance:{pano}", "name": name, "kind": "entrance", "aliases": ["entrance", "front door"],
                    "serves": ["exit"], "description": "", "map": sid, "floor": g.nodes[pano].floor, "rooms": [],
                    "panos": [pano], "entry_panos": [pano], "polygon": None, "source": "vexcel",
                    "confidence": 0.8 if p.get("confirmed") else 0.4,
                    "outdoor_node": p.get("outdoor_node"), "width_m": p.get("width_m"), "step": p.get("step")})
    og = getattr(site, "og", None)
    for n in (og.nodes.values() if og else []):
        if any("crossing" in e.tags for e in og.adj[n.id].values()):
            out.append({"id": f"outdoor:{n.id}", "name": "street crossing", "kind": "outdoor_zone",
                        "aliases": ["crossing", "crosswalk"], "serves": ["crossing"], "description": "",
                        "map": f"outdoor:{sid}", "floor": n.floor, "rooms": [], "panos": [n.id],
                        "entry_panos": [n.id], "polygon": None, "source": "osm", "confidence": 0.6})
            break
    return out


def _costar(sid):
    try:
        from .agents.tools import costar_record
        rec = json.loads(costar_record.invoke({"sid": sid}))
    except Exception:
        return None
    if rec.get("error"):
        return None
    return rec.get("record") if isinstance(rec.get("record"), dict) else rec


def seed(sid, building=False):
    """draft catalog for one scan, or for every joined scan of the building (ids namespaced sid:...)."""
    if building:
        from .building import building_graph
        g = building_graph(sid)
    else:
        from .agents.tools import graph
        g = graph(sid)
    return seed_from_graph(g, sid, costar=_costar(sid))


# ---------- load: agent catalog over the seed ----------

def places_path(sid):
    from .agents.tools import dossier_dir
    return dossier_dir(sid) / "places.json"


def place_files(sid):
    """places.json plus per-floor fragments places/<floor>.json (subagents run in parallel, one file each)."""
    main = places_path(sid)
    frags = sorted((main.parent / "places").glob("*.json")) if (main.parent / "places").exists() else []
    return ([main] if main.exists() else []) + frags


def read_agent(sid):
    """the agent's catalog, merged across files by id. a broken file is reported but doesn't hide the others."""
    files = place_files(sid)
    if not files:
        return None, None
    merged, order, errs = {}, [], []
    for p in files:
        name = p.name if p.parent.name != "places" else f"places/{p.name}"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as ex:
            errs.append(f"{name} is not valid json ({ex}); rewrite it with write_file")
            continue
        items = data if isinstance(data, list) else (data or {}).get("places") or []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("rooms"), list):
                # agents often write place ids ("room:r0") where raw room ids ("r0") belong
                it = {**it, "rooms": [r[5:] if isinstance(r, str) and r.startswith("room:") else r for r in it["rooms"]]}
            if not isinstance(it, dict) or not it.get("id"):
                if isinstance(it, dict):
                    merged.setdefault(f"__noid{len(order)}", it)
                    order.append(f"__noid{len(order)}")
                continue
            if it["id"] not in merged:
                order.append(it["id"])
                merged[it["id"]] = dict(it)
            else:
                merged[it["id"]].update({k: v for k, v in it.items() if v not in (None, "", [])})
    return {"places": [merged[k] for k in order]}, ("; ".join(errs) or None)


WORD_FIELDS = ("name", "aliases", "serves", "description", "kind", "confidence")


def merge(seeded, agent, g):
    """agent wins on words; geometry always from the seed or derived from pano ids that exist."""
    out = {p["id"]: dict(p) for p in seeded["places"]}
    for a in (agent or {}).get("places") or []:
        pid = a.get("id")
        if not pid:
            continue
        a = {k: v for k, v in a.items() if k != "polygon"}  # never authored
        if pid in out:
            base = out[pid]
            for k in WORD_FIELDS:
                if a.get(k) not in (None, "", []):
                    base[k] = a[k]
            base["source"] = "agent" if base.get("source") == "matterport" else base["source"]
            base["unnamed"] = False if a.get("name") else base.get("unnamed", False)
            continue
        p = {"aliases": [], "serves": [], "description": "", "polygon": None, "source": "agent",
             "confidence": 0.6, **a}
        if p.get("kind") == "feature":
            anc = p.get("anchor") or {}
            pano = anc.get("pano_id")
            if pano not in g.nodes:
                continue  # an anchor we can't place is no place at all
            p["panos"], p["entry_panos"] = [pano], [pano]
            p["floor"] = g.nodes[pano].floor
            n = g.nodes[pano]
            p.setdefault("rooms", [n.room] if n.room else [])
        elif p.get("rooms"):
            ids = sorted(n.id for n in g.nodes.values() if n.room in set(p["rooms"]))
            p["panos"] = ids
            p["entry_panos"] = _entry_panos(g, ids) if ids else []
            p["floor"] = g.nodes[ids[0]].floor if ids else p.get("floor")
        else:
            ids = [i for i in p.get("panos") or [] if i in g.nodes]
            p["panos"] = ids
            p["entry_panos"] = [i for i in p.get("entry_panos") or [] if i in g.nodes] or ids
        out[pid] = p
    meta = {**seeded.get("meta", {}), **((agent or {}).get("meta") or {}), "agent": bool(agent)}
    return {"places": list(out.values()), "meta": meta}


def load(sid, building=False):
    from .agents.tools import graph
    s = seed(sid, building)
    a, _ = read_agent(sid)
    g = graph(sid) if not building else None
    if g is None:
        from .building import building_graph
        g = building_graph(sid)
    return merge(s, a, g)


def catalog_for_graph(g):
    sid = getattr(g, "sid", None)
    s = seed_from_graph(g, sid, costar=_costar(sid) if sid else None)
    a = read_agent(sid)[0] if sid else None
    return merge(s, a, g)


# ---------- resolve ----------

def _singular(w):
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def normalize(q):
    s = str(q).strip().lower()
    s = re.sub(r"[?!.,]+$", "", s).strip()
    for _ in range(3):
        s2 = LEAD_RE.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\b(?:here|please|now|from here)\s*$", "", s).strip()
    s = re.sub(r"^(?:get|go|find|use|have)\s+", "", s)
    s = re.sub(r"^(?:the|a|an|my|some|any)\s+", "", s).strip()
    return " ".join(_singular(w) for w in s.split())


def _tokens(s):
    return {_singular(w) for w in re.findall(r"[a-z0-9']+", str(s).lower())} - GENERIC


def intents(q):
    low = " " + str(q).lower() + " "
    found = []
    for tag, words in INTENTS.items():
        if any(re.search(r"\b" + re.escape(w) + r"s?\b", low) for w in words):
            found.append(tag)
    return found


def _fuzzy_overlap(qt, pt):
    if not qt or not pt:
        return 0.0
    hit = 0
    for a in qt:
        if a in pt or any(difflib.SequenceMatcher(None, a, b).ratio() >= 0.85 for b in pt):
            hit += 1
    return hit / len(qt)


def score(place, nq, its):
    """(score, why). exact name > alias > need (serves) > partial > fuzzy token overlap."""
    name = place.get("name") or ""
    names = [normalize(name), normalize(name.split(",")[0])]
    aliases = [normalize(a) for a in place.get("aliases") or []]
    if nq and nq in names:
        s, why = 100, "name"
    elif nq and nq in aliases:
        s, why = 90, "alias"
    elif its and set(its) & set(place.get("serves") or []):
        s, why = 70, f"serves {', '.join(sorted(set(its) & set(place['serves'])))}"
        if place.get("kind") == "feature":
            s += 5  # the water fountain itself beats the kitchen it's in
    elif nq and any(len(nq) > 3 and (nq in x or (len(x) > 3 and x in nq)) for x in names + aliases):
        s, why = 60, "partial name"
    else:
        ov = max([_fuzzy_overlap(_tokens(nq), _tokens(x)) for x in names + aliases] or [0])
        s, why = 40 * ov, "similar words"
    if place.get("unnamed"):
        s *= 0.5
    if place.get("kind") == "tenant" and not place.get("location_known"):
        s *= 0.6
    return s, why


def _summary(p):
    return {k: p.get(k) for k in ("id", "name", "kind", "aliases", "serves", "description", "map", "floor",
                                  "anchor", "confidence") if p.get(k) not in (None, [], "")}


def goal_nodes(place):
    if place.get("kind") == "feature" and place.get("anchor"):
        return [place["anchor"]["pano_id"]]
    if place.get("kind") == "floor":
        return list(place.get("entry_panos") or place.get("panos") or [])
    return list(place.get("panos") or place.get("entry_panos") or [])


def _route_m(g, here, nodes, profile):
    if not here or not nodes:
        return None
    from .navigator import snap
    from .router import NoRoute, plan
    x, y, floor = here[:3]
    start, d = snap(g, x, y, floor)
    try:
        return round(plan(g, start, set(nodes), profile).length + d, 1)
    except NoRoute:
        return None


def _container(place, others):
    """the room a feature sits in, if that room is also a candidate."""
    if place.get("kind") != "feature":
        return None
    rooms = set(place.get("rooms") or [])
    return next((o["id"] for o in others if o["id"] != place["id"] and set(o.get("rooms") or []) & rooms
                 and o.get("kind") == "room"), None)


NEED_WORDS = {"restroom": "bathroom", "water": "place to get water", "exit": "exit", "help_desk": "help desk",
              "food": "place to get food", "seating": "place to sit", "elevator": "elevator", "stairs": "staircase"}


def base_name(name):
    """'Bathroom 2, by the entrance' -> 'Bathroom'; 'Elevator, Floor 1' -> 'Elevator'."""
    b = str(name).split(",")[0].strip()
    return re.sub(r"\s+\d+$", "", b).strip() or b


def closest_rooms(cat, q, n=4):
    """up to n room names, deduped by base name, most similar to q first. no features, no unnamed spaces."""
    rooms = [p for p in cat["places"] if p.get("kind") == "room" and not p.get("unnamed") and goal_nodes(p)
             and base_name(p["name"]).lower() not in ("other", "void", "unknown")]
    by_base = {}
    for p in rooms:
        by_base.setdefault(base_name(p["name"]), p)
    names = list(by_base)
    ql = str(q).lower()
    names.sort(key=lambda b: (-difflib.SequenceMatcher(None, ql, b.lower()).ratio(), b))
    return names[:n]


def no_match_text(q, its, suggestions):
    need = next((NEED_WORDS[i] for i in its if i in NEED_WORDS), None)
    head = f"I don't have a {need} on record in this building." if need else f"There's no {q} on record here."
    if suggestions:
        return f"{head} Closest matches: {', '.join(suggestions)}."
    return head


def resolve(src, query, here=None, profile="blind", catalog=None):
    """src: a sid or a WalkGraph. here: (x, y, floor) to rank by route length. returns a dict, never raises."""
    from .instruct import meters
    if isinstance(src, str):
        from .agents.tools import graph
        g = graph(src)
        cat = catalog or load(src)
    else:
        g = src
        cat = catalog or catalog_for_graph(g)
    nq = normalize(query)
    its = intents(query)
    cands = []
    for p in cat["places"]:
        if not goal_nodes(p) and p.get("kind") != "tenant":
            continue
        s, why = score(p, nq, its)
        if s >= 30:
            cands.append({"place": p, "score": round(s, 1), "why": why, "route_m": None})
    for c in cands:
        c["route_m"] = _route_m(g, here, goal_nodes(c["place"]), profile)
    # reachable before unreachable, then score, then distance
    cands.sort(key=lambda c: (-c["score"], here is not None and c["route_m"] is None,
                              c["route_m"] if c["route_m"] is not None else 1e9))
    out = {"query": query, "normalized": nq, "intents": its, "candidates": [
        {**_summary(c["place"]), "score": c["score"], "why": c["why"], "route_m": c["route_m"]} for c in cands[:8]],
        "best": None, "ambiguous": False, "question": None}
    if not cands:
        out["suggestions"] = closest_rooms(cat, nq or query)
        out["message"] = no_match_text(nq or query, its, out["suggestions"])
        return out
    top = cands[0]["score"]
    tier = [c for c in cands if c["score"] >= top - 5]
    tier_places = [c["place"] for c in tier]
    distinct = [c for c in tier if not _container(c["place"], tier_places)]
    if len(distinct) > 1 and here is not None:
        reach = [c for c in distinct if c["route_m"] is not None]
        if len(reach) == 1:
            distinct = reach
        elif len(reach) > 1:
            reach.sort(key=lambda c: c["route_m"])
            a, b = reach[0]["route_m"], reach[1]["route_m"]
            if b - a >= 10 or b >= 1.5 * max(a, 1.0):
                out["best"] = reach[0]["place"]
                out["why"] = f"nearest of {len(reach)} ({meters(a)})"
                return out
            distinct = reach
    if len(distinct) > 1:
        out["ambiguous"] = True
        opts = []
        for c in distinct[:4]:
            p = c["place"]
            where = p["name"]
            if c["route_m"] is not None:
                where += f", {meters(c['route_m'])}"
            opts.append(where)
        need = next((NEED_WORDS[i] for i in its if i in NEED_WORDS), None)
        kind = need or (nq or "place")
        out["question"] = (f"There are {len(distinct)} {plural(kind)}: " if need else
                           f"There are {len(distinct)} places that match {kind}: ") + "; ".join(opts) + ". Which one?"
        out["options"] = [_summary(c["place"]) for c in distinct[:4]]
        return out
    best = cands[0] if cands[0]["place"] in [c["place"] for c in distinct] else distinct[0]
    # prefer the feature over its room when both matched the need
    feat = next((c for c in tier if c["place"].get("kind") == "feature"), None)
    out["best"] = (feat or best)["place"]
    out["why"] = (feat or best)["why"]
    return out


# ---------- where am i ----------

def where_am_i(g, x, y, floor, catalog=None):
    cat = catalog or catalog_for_graph(g)
    rooms = [p for p in cat["places"] if p["kind"] == "room" and p.get("floor") == floor and p.get("polygon")]
    inside = [p for p in rooms if point_in_poly((x, y), p["polygon"])]
    inside.sort(key=lambda p: (p.get("unnamed", False), _area(p["polygon"])))
    place = inside[0] if inside else None
    if place is None:
        from .navigator import snap
        nid, _ = snap(g, x, y, floor)
        room = g.nodes[nid].room
        place = next((p for p in cat["places"] if p["kind"] == "room" and room in (p.get("rooms") or [])), None)
    if place is None:
        return {"place": None, "text": "I'm not sure which room you're in."}
    name = place["name"]
    feats = [p for p in cat["places"] if p["kind"] == "feature" and set(p.get("rooms") or []) & set(place.get("rooms") or [])]
    # the scan's catch-all labels ("Other 1", "Void") mean nothing to a traveller: say what's around them instead
    generic = place.get("unnamed") or re.match(r"^(other|void|unknown|room)\b(?: \d+)?", name.split(",")[0].strip(), re.I)
    if generic:
        fl = name.split(",", 1)[1].strip() if "," in name else None
        # "Water fountain, east corridor" -> "the water fountain in the east corridor"
        near = " and ".join("the " + (f["name"][0].lower() + f["name"][1:]).replace(", ", " in the ", 1)
                            for f in feats[:2])
        text = ("You're" + (f" on {fl}" if fl else "") + (f", by {near}" if near else "")
                + ".") if (fl or near) else "You're in an unnamed space."
    else:
        text = f"You're in {the(place)}."
    if place.get("description") and not generic:
        text += " " + place["description"]
    if feats and not generic:
        text += " Here: " + ", ".join(f["name"] for f in feats[:3]) + "."
    return {"place": _summary(place), "text": text}


# ---------- validation (what the place-indexer is held to) ----------

def validate(sid, building=False):
    agent, err = read_agent(sid)
    if err:
        return {"ok": False, "errors": [err]}
    if agent is None:
        return {"ok": False, "errors": ["no /dossier/places.json yet"]}
    from .agents.tools import graph
    g = graph(sid)
    s = seed_from_graph(g, sid, costar=_costar(sid))
    seed_ids = {p["id"] for p in s["places"]}
    errors, warns = [], []
    seen_ids = set()
    for i, p in enumerate(agent.get("places") or []):
        tag = f"places[{i}] {p.get('id', '?')}"
        if not p.get("id") or (not p.get("name") and p.get("id") not in seed_ids):
            errors.append(f"{tag}: needs an id, and a name unless it refines a place from place_seed")
            continue
        if p["id"] in seen_ids:
            errors.append(f"{tag}: duplicate id")
        seen_ids.add(p["id"])
        if p.get("polygon") is not None:
            errors.append(f"{tag}: don't write polygons; geometry comes from the data")
        kind = p.get("kind") or next((x["kind"] for x in s["places"] if x["id"] == p["id"]), None)
        if kind not in KINDS:
            errors.append(f"{tag}: kind {kind!r} not in {sorted(KINDS)}")
        if p["id"].startswith(("room:", "floor:")) and p["id"] not in seed_ids:
            errors.append(f"{tag}: no such room/floor in this scan (use ids from place_seed)")
        if kind == "feature":
            anc = p.get("anchor") or {}
            if anc.get("pano_id") not in g.nodes:
                errors.append(f"{tag}: feature needs anchor.pano_id from list_panos")
            if not isinstance(anc.get("bearing_deg"), (int, float)):
                errors.append(f"{tag}: feature needs anchor.bearing_deg (model frame, degrees)")
        for r in p.get("rooms") or []:
            if r not in g.rooms:
                errors.append(f"{tag}: room {r} not in this scan")
        for pano in (p.get("panos") or []) + (p.get("entry_panos") or []):
            if pano not in g.nodes:
                errors.append(f"{tag}: pano {pano} not in this scan")
        for sv in p.get("serves") or []:
            if sv not in SERVES:
                warns.append(f"{tag}: serves {sv!r} is not in the vocabulary {sorted(SERVES)}")
        if len((p.get("description") or "").split()) > 25:
            warns.append(f"{tag}: description over 25 words")
        if p.get("source") and p["source"] not in SOURCES:
            warns.append(f"{tag}: source {p['source']!r} unknown")
    if errors:
        return {"ok": False, "errors": errors[:40], "warnings": warns[:20]}
    cat = merge(s, agent, g)
    # one alias used for two different locations confuses the traveller
    owners = {}
    for p in cat["places"]:
        for a in [p.get("name", "")] + list(p.get("aliases") or []):
            key = normalize(a)
            if key:
                owners.setdefault(key, set()).add(p["id"])
    for key, ids in owners.items():
        locs = {frozenset(next(x for x in cat["places"] if x["id"] == i).get("rooms") or [i]) for i in ids}
        if len(locs) > 1 and len(ids) > 1:
            warns.append(f"'{key}' names {len(ids)} different places: {sorted(ids)}")
    unnamed = [p["id"] for p in cat["places"] if p.get("unnamed")]
    return {"ok": True, "warnings": warns[:30], "places": len(cat["places"]),
            "features": sum(p["kind"] == "feature" for p in cat["places"]),
            "named_by_agent": sum(1 for p in cat["places"] if p.get("source") == "agent"),
            "still_unnamed": unnamed}


# ---------- a tool the runtime agents share ----------

def find_place_tool(nav, g):
    from langchain_core.tools import tool

    @tool(parse_docstring=True)
    def find_place(query: str) -> str:
        """Look up a place by what the traveller calls it or needs ("the kitchenette", "somewhere to get water",
        "the way out"). Returns ranked named places with route metres from where the traveller is, or a question
        when several places match. Use the returned name as the goal; never invent places.

        Args:
            query: The traveller's words.
        """
        here = getattr(nav, "pos", None)
        r = resolve(g, query, here=here, profile=getattr(nav, "profile", "blind"))
        if r.get("best"):
            r["best"] = _summary(r["best"]) | {"goal_nodes": goal_nodes(r["best"])}
        return json.dumps(r, default=str)

    return find_place


def plan_to(g, start, query, profile="blind", here=None):
    """route from a pano to whatever the query means; surfaces ambiguity instead of guessing."""
    from .router import NoRoute, plan
    r = resolve(g, query, here=here, profile=profile)
    if r["ambiguous"]:
        return {"ambiguous": True, "question": r["question"], "options": r.get("options")}
    if not r["best"]:
        return {"error": r.get("message"), "suggestions": r.get("suggestions")}
    nodes = goal_nodes(r["best"])
    try:
        route = plan(g, start, set(nodes), profile)
    except NoRoute as ex:
        return {"error": str(ex), "place": _summary(r["best"])}
    return {"place": _summary(r["best"]), "route": route, "why": r.get("why")}


