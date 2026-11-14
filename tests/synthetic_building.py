"""a second scan of the synthetic building: the street-level lobby, in its own coordinate frame.

its elevator (p_EL) goes up to the main scan's floor 1 elevator (synthetic:p_E1). nothing lines the two
frames up; the portal between the elevators is the only link.
"""
from tests.synthetic import _Builder


def lobby():
    b = _Builder()
    b.floor("g", 0)
    b.floors["g"]["label"] = "Street level"
    # vestibule with the street doors on its south wall, opening into the lobby hall
    b.room("g", "Entrance Vestibule", [(0, 0), (1.5, 0), (2.7, 0), (4, 0), (4, 1), (4, 2.2), (4, 3), (0, 3)],
           {1: "entrance", 4: "door"})
    b.room("g", "Lobby", [(4, 0), (12, 0), (12, 6), (11.6, 6), (10.2, 6), (4, 6), (4, 3), (4, 2.2), (4, 1)],
           {3: "opening", 7: "door"})
    b.room("g", "Elevator", [(10.2, 6), (11.6, 6), (11.6, 8), (10.2, 8)], {0: "opening"})
    for lab, x, y in [("V1", 2.0, 1.5), ("H1", 6.0, 1.6), ("H2", 9.0, 3.0), ("H3", 10.9, 4.6), ("EL", 10.9, 7.0)]:
        b.pano(lab, x, y, 1.5, "g")
    m = b.model()
    m["id"], m["name"] = "synthetic-lobby", "Lobby"
    return m
