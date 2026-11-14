"""the belt's servos on a pca9685, driven by our own cues (NUDGE_BAND=pca)."""
from fastapi.testclient import TestClient

from nudge import bandserve, belt, walkgraph
from nudge.band import PcaBand
from nudge.server import app
from tests.synthetic import building

c = TestClient(app)


class Bus:
    def __init__(self):
        self.bytes, self.blocks = [], []

    def write_byte_data(self, addr, reg, val):
        self.bytes.append((addr, reg, val))

    def write_i2c_block_data(self, addr, reg, data):
        self.blocks.append((addr, reg, list(data)))

    def close(self):
        pass


def test_pca_band_runs_at_50hz_and_drives_its_two_channels():
    bus = Bus()
    b = PcaBand(channels=(3, 4), bus=bus)
    assert (0x40, 0xFE, 121) in bus.bytes                      # 25 MHz / (4096 * 50) - 1
    assert [blk[2] for blk in bus.blocks] == [[0, 0, 0, 0x10]] * 2   # both start fully off
    bus.blocks.clear()
    b.apply(30.0, None)
    (_, reg, d), (_, reg2, d2) = bus.blocks
    assert reg == 0x06 + 4 * 3 and d[2] | d[3] << 8 == round((0.5 + 1.9 * 30 / 90) * 50 / 1000 * 4096)
    assert reg2 == 0x06 + 4 * 4 and d2 == [0, 0, 0, 0x10]     # detached: silent
    b.apply(30.0, None)
    assert len(bus.blocks) == 2                                # nothing changed, nothing written


def start(monkeypatch_env=None):
    g = walkgraph.build(building())
    s = g.nodes["p_K3"]
    r = c.post("/api/nav/start", json={"sid": "synthetic", "to": "Conference Room", "profile": "blind",
                                       "x": s.x, "y": s.y, "floor": s.floor, "heading": 0.0})
    assert r.status_code == 200, r.text
    return r.json()["session"]


def test_each_new_walk_takes_the_band_over(monkeypatch):
    monkeypatch.setenv("NUDGE_BAND", "dry")
    first = start()
    assert bandserve.status()["session"] == first
    second = start()
    assert bandserve.status()["session"] == second             # the second trip taps too


def test_with_our_band_the_belt_runtime_stays_out(monkeypatch):
    monkeypatch.setenv("NUDGE_BAND", "dry")
    monkeypatch.setenv("NUDGE_BELT_URL", "http://127.0.0.1:9")
    belt.detach()
    start()
    assert belt._state["session"] is None                      # no route sent, nothing followed
