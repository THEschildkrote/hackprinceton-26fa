"""a two-floor building in the model api's shape: lobby, office, kitchen, stairs, elevator, conference room."""


class _Builder:
    def __init__(self):
        self.floors, self.rooms, self.panos = {}, [], []
        self._v = {}

    def floor(self, fid, seq):
        self.floors[fid] = {"id": fid, "label": f"Floor {seq + 1}", "sequence": seq, "vertices": [], "edges": []}

    def _vert(self, fid, x, y):
        k = (fid, x, y)
        if k not in self._v:
            vid = f"v{len(self._v)}"
            self._v[k] = vid
            self.floors[fid]["vertices"].append({"id": vid, "position": {"x": x, "y": y}})
        return self._v[k]

    def room(self, fid, label, pts, types=None):
        types = types or {}
        f, ids = self.floors[fid], []
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            eid = f"e{fid}_{len(f['edges'])}"
            f["edges"].append({"id": eid, "type": types.get(i, "wall"), "thickness": 0.15,
                               "vertices": [{"id": self._vert(fid, *a)}, {"id": self._vert(fid, *b)}]})
            ids.append({"id": eid})
        self.rooms.append({"id": f"r{len(self.rooms)}", "floor": {"id": fid},
                           "classifications": [{"label": label, "confidence": 1.0}], "boundary": {"edges": ids}})

    def pano(self, label, x, y, z, fid=None):
        p = {"id": f"p_{label}", "label": label, "position": {"x": x, "y": y, "z": z}}
        if fid:
            p["floor"] = {"id": fid}
        self.panos.append(p)

    def model(self):
        return {"id": "synthetic", "name": "Synthetic", "floors": list(self.floors.values()),
                "rooms": self.rooms, "panoLocations": self.panos}


def building(with_floor_ids=True, kitchen_door_w=1.0):
    """kitchen_door_w: clear width of the lobby-kitchen door (centred at x=6.5), to test narrow doors."""
    ka, kb = 6.5 - kitchen_door_w / 2, 6.5 + kitchen_door_w / 2
    b = _Builder()
    b.floor("f1", 0)
    b.floor("f2", 1)
    # floor 1
    b.room("f1", "Lobby", [(0, 0), (8, 0), (8, 2), (8, 3), (8, 6), (kb, 6), (ka, 6), (0, 6)], {2: "door", 5: "door"})
    b.room("f1", "Office", [(8, 0), (16, 0), (16, 1), (16, 2), (16, 6), (14, 6), (12, 6), (8, 6), (8, 3), (8, 2)],
           {2: "opening", 5: "opening", 8: "door"})
    b.room("f1", "Elevator", [(12, 6), (14, 6), (14, 8), (12, 8)], {0: "opening"})
    b.room("f1", "Kitchen", [(0, 6), (ka, 6), (kb, 6), (8, 6), (8, 12), (0, 12)], {1: "door"})
    b.room("f1", "Stairs", [(16, 0), (19, 0), (19, 3), (16, 3), (16, 2), (16, 1)], {4: "opening"})
    # floor 2
    b.room("f2", "Conference Room", [(0, 0), (16, 0), (16, 1), (16, 2), (16, 6), (14, 6), (12, 6), (0, 6)],
           {2: "opening", 5: "opening"})
    b.room("f2", "Elevator", [(12, 6), (14, 6), (14, 8), (12, 8)], {0: "opening"})
    b.room("f2", "Stairs", [(16, 0), (19, 0), (19, 3), (16, 3), (16, 2), (16, 1)], {4: "opening"})

    f1 = "f1" if with_floor_ids else None
    f2 = "f2" if with_floor_ids else None
    for lab, x, y in [("L1", 2, 1.5), ("L2", 5, 1.5), ("L3", 2, 4.5), ("L4", 6.5, 2.5), ("L5", 5, 4.5),
                      ("L6", 6.5, 5.2), ("O1", 9.5, 2.5), ("O2", 12, 2), ("O3", 14.5, 1.5), ("O4", 12, 4.5),
                      ("O5", 14.8, 4.5), ("S1", 17.5, 1.5), ("E1", 13, 7), ("K1", 6.5, 7), ("K2", 4, 9),
                      ("K3", 2, 11)]:
        b.pano(lab, x, y, 1.5, f1)
    for lab, x, y in [("S2", 17.5, 1.5), ("E2", 13, 7), ("C1", 14, 1.5), ("C2", 12, 4.5), ("C3", 9, 3),
                      ("C4", 5, 3), ("C5", 2, 3)]:
        b.pano(lab, x, y, 5.5, f2)
    return b.model()
