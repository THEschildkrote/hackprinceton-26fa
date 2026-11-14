import io
import math

import numpy as np
import pytest
from PIL import Image

from nudge import perceive as P
from tests.synthetic_perceive import GLASS, SCONCE

COLORS = [(255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 0, 0)]


def cube(n=64):
    faces = [np.full((n, n, 3), c, np.uint8) for c in COLORS]
    faces[1][:, n // 2:] = (255, 0, 255)   # face 1: left half red, right half magenta
    return faces


def centre(img):
    h, w = img.shape[:2]
    return tuple(int(v) for v in img[h // 2, w // 2])


@pytest.mark.parametrize("yaw,pitch,face", [(0, 0, 1), (-90, 0, 2), (270, 0, 2), (180, 0, 3), (90, 0, 4),
                                             (0, 89.9, 0), (0, -89.9, 5), (37, 89.9, 0)])
def test_skybox_faces(yaw, pitch, face):
    img = P.render_skybox(cube(), yaw, pitch, 60, 33, face1_yaw_deg=0.0)
    want = COLORS[face] if face != 1 else None
    if want:
        assert centre(img) == want
    else:
        assert centre(img) in (COLORS[1], (255, 0, 255))


def test_skybox_left_right_and_offset():
    img = P.render_skybox(cube(), 0, 0, 60, 64, face1_yaw_deg=0.0)
    assert tuple(img[32, 5]) == (255, 0, 0) and tuple(img[32, 58]) == (255, 0, 255)
    # if face 1 looks along model +y, yaw 90 shows face 1 and yaw 0 shows face 2 (to its right)
    assert centre(P.render_skybox(cube(), 0, 0, 60, 33, face1_yaw_deg=90.0)) == COLORS[2]
    assert centre(P.render_skybox(cube(), 90, 0, 60, 33, face1_yaw_deg=90.0)) in (COLORS[1], (255, 0, 255))


def test_rays_frame():
    d = P.rays(90, 0, 60, 3, 3)
    assert np.allclose(d[1, 1], [0, 1, 0], atol=1e-9)
    # the right side of a view looking along +y points toward +x
    assert d[1, 2, 0] > 0


def test_look_synthetic():
    jpg, meta = P.look("synthetic", "p_L1", 0, size=256)
    im = Image.open(io.BytesIO(jpg))
    assert im.format == "JPEG" and im.size == (256, 256)
    assert meta["room_label"] == "Lobby" and meta["floor"] == "f1" and meta["pano_id"] == "p_L1"
    # a label works too
    assert P.look("synthetic", "L1", 90, size=64)[1]["pano_id"] == "p_L1"


def test_calibrate_synthetic():
    c = P.calibrate("synthetic", "p_L1")
    walls = {v["yaw_deg"]: v["expected_wall_m"] for v in c["views"]}
    # L1 is at (2, 1.5) in a lobby spanning x 0..8, y 0..6
    assert walls == {0: 6.0, 90: 4.5, 180: 2.0, 270: 1.5}


def test_overhang_finds_only_the_sconce():
    found = P.overhang_candidates("synthetic", "f1")
    assert len(found) == 1, found
    c = found[0]
    s = SCONCE
    assert s["x0"] <= c["x"] <= s["x1"] and s["y0"] - 0.1 <= c["y"] <= s["y1"]
    assert abs(c["z_low_m"] - s["h0"]) < 0.06 and abs(c["z_high_m"] - s["h1"]) < 0.06
    assert c["on_edges"] and c["nearest_pano"]
    assert P.overhang_candidates("synthetic", "f2") == []


def test_overhang_bearing_points_at_it():
    c = P.overhang_candidates("synthetic", "f1")[0]
    n = P._graph("synthetic").nodes[c["nearest_pano"]]
    want = math.degrees(math.atan2(c["y"] - n.y, c["x"] - n.x))
    assert abs((c["bearing_deg"] - want + 180) % 360 - 180) < 0.2
    assert abs(c["distance_m"] - math.hypot(c["x"] - n.x, c["y"] - n.y)) < 0.02
    # and the pano can actually see it
    k = (c["distance_m"] - 0.15) / c["distance_m"]
    assert not P._graph("synthetic").blocked_line(n.xy, (n.x + k * (c["x"] - n.x), n.y + k * (c["y"] - n.y)), "f1")


def test_glass_finds_only_the_glass_wall():
    found = P.glass_suspects("synthetic", "f1")
    assert len(found) == 1, found
    g = found[0]
    assert {tuple(g["a"]), tuple(g["b"])} == {GLASS["a"], GLASS["b"]}
    assert g["support_ratio"] < 0.1
    n = P._graph("synthetic").nodes[g["nearest_pano"]]
    mx, my = 8.0, 4.5
    assert abs(g["bearing_deg"] - math.degrees(math.atan2(my - n.y, mx - n.x))) < 0.2
    assert P.glass_suspects("synthetic", "f2") == []


def test_door_header_is_not_an_overhang():
    # headers start at 2.0 m over every doorway in the synthetic cloud; none may be reported
    for c in P.overhang_candidates("synthetic", "f1", max_items=100):
        assert c["z_low_m"] < P.HEADER_Z


def test_floor_levels():
    lv = P.floor_levels(P._graph("synthetic"), P.cloud("synthetic"))
    assert abs(lv["f1"][0]) < 0.03 and abs(lv["f2"][0] - 4.0) < 0.03
    assert lv["f1"][1] < lv["f2"][0]


def test_parse_xyz_keeps_xyz():
    a = P._parse_xyz([b"1 2 3 255 0 0", b"", b"4 5 6 0 255 0", b"bad line"])
    assert a.shape == (2, 3) and a[1].tolist() == [4, 5, 6]


def test_aerial_synthetic():
    jpg, meta = P.aerial("synthetic", 30, 256)
    assert Image.open(io.BytesIO(jpg)).size == (256, 256)
    assert meta["gsd_m"] == round(60 / 256, 4)
