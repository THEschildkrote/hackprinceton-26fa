import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from nudge.actuators import MockDriver
from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.navigator import Navigator
from nudge.runtime import Runtime
from nudge.sensors.phone import PhoneSource
from nudge.sensors.sim import SimSource
from nudge.server import build_app

ROOT = Path(__file__).resolve().parent.parent
L_ROUTE = {"waypoints": [[0, 0], [0, 5], [5, 5]]}


def serve(source, test):
    """Run `test(client, runtime)` against a live app on a random local port."""

    async def main():
        cfg = Config.from_dict({})
        nav = Navigator(cfg, EventLog(keep=True))
        driver = MockDriver(cfg.actuators)
        runtime = Runtime(cfg, nav, source, driver, ROOT / "routes")
        loop_task = asyncio.create_task(runtime.run())
        client = TestClient(TestServer(build_app(runtime)))
        await client.start_server()
        try:
            return await test(client, runtime)
        finally:
            await client.close()
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
            await driver.close()

    return asyncio.run(main())


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def reading(heading, steps, **extra):
    return {"type": "sensor", "heading_deg": heading, "compass_deg": heading, "step_count": steps, **extra}


def test_phone_over_websocket_drives_a_walk():
    async def test(client, runtime):
        assert (await client.post("/route", json=L_ROUTE)).status == 200
        refused = await client.post("/calibrate")
        assert refused.status == 409 and "sensor" in (await refused.json())["error"]

        ws = await client.ws_connect("/ws")
        await ws.send_json(reading(200.0, 5))
        await wait_for(lambda: runtime.nav.samples >= 1)
        cal = await (await client.post("/calibrate")).json()
        assert cal["ok"] and abs(cal["compass_deg"] - 200.0) < 1e-6 and cal["heading_deg"] == 0.0
        assert (await client.post("/start")).status == 200

        await ws.send_json(reading(245.0, 6))   # one step, 45 deg off to the right
        await wait_for(lambda: runtime.nav.samples >= 2)
        state = await (await client.get("/state")).json()
        assert state["state"] == "NAVIGATING" and state["calibrated"] and state["sensor_ok"]
        assert state["source"] == "phone" and state["transport"] == "ws" and not state["sim"]
        assert abs(state["pose"]["heading_deg"] - 45.0) < 1e-6
        assert state["sensor"]["step_count"] == 6
        assert state["taps"][-1]["kind"] == "tap" and state["taps"][-1]["side"] == "left"
        assert state["route"]["waypoints"] == [[0, 0], [0, 5], [5, 5]]

        assert (await client.post("/stop")).status == 200
        state = await (await client.get("/state")).json()
        assert state["state"] == "STOPPED" and state["taps"][-1]["kind"] == "stop"
        await ws.close()

    serve(PhoneSource(), test)


def test_http_post_is_an_equivalent_sensor_transport():
    async def test(client, runtime):
        ok = await client.post("/sensor", json=reading(10.0, 0))
        assert ok.status == 200
        await wait_for(lambda: runtime.nav.samples >= 1)
        assert runtime.source.transport == "post"
        assert (await client.post("/sensor", json={"heading_deg": None, "step_count": 1})).status == 400
        assert (await client.post("/sensor", data="not json")).status == 400
        assert runtime.nav.samples == 1

    serve(PhoneSource(), test)


def test_a_second_phone_is_told_it_is_being_ignored():
    async def test(client, runtime):
        first = await client.ws_connect("/ws")
        await first.send_json(reading(10.0, 5))
        await wait_for(lambda: runtime.nav.samples >= 1)

        second = await client.ws_connect("/ws")
        await second.send_json(reading(200.0, 80))
        assert await second.receive_json(timeout=2) == {"busy": True}
        posted = await client.post("/sensor", json=reading(200.0, 80))
        assert posted.status == 409 and (await posted.json())["error"] == "busy"
        assert runtime.nav.samples == 1 and runtime.nav.last.heading_deg == 10.0

        # The first phone goes quiet; after takeover_s the second one is the sensor.
        await asyncio.sleep(0.3)
        await second.send_json(reading(200.0, 81))
        assert await second.receive_json(timeout=2) == {"busy": False}
        await wait_for(lambda: runtime.nav.samples >= 2)
        assert runtime.nav.last.heading_deg == 200.0
        assert runtime.nav.last.step_count == 5          # its own count (81) is not a 76-step walk
        await first.close()
        await second.close()

    serve(PhoneSource(takeover_s=0.2), test)


def test_readings_the_phone_calls_old_are_not_used():
    async def test(client, runtime):
        old = await client.post("/sensor", json=reading(10.0, 0, age_ms=4000))
        assert old.status == 400 and (await old.json())["error"] == "old"
        assert runtime.nav.samples == 0
        assert (await client.post("/sensor", json=reading(10.0, 0, age_ms=80))).status == 200

    serve(PhoneSource(), test)


def test_route_and_calibrate_are_refused_mid_walk():
    async def test(client, runtime):
        await client.post("/route", json=L_ROUTE)
        await client.post("/sensor", json=reading(0.0, 0))
        await wait_for(lambda: runtime.nav.samples >= 1)
        assert (await client.post("/calibrate")).status == 200
        assert (await client.post("/start")).status == 200
        for path, body in (("/calibrate", None), ("/route", {"name": "demo_L"})):
            refused = await client.post(path, json=body)
            assert refused.status == 409 and "stop first" in (await refused.json())["error"]
        state = await (await client.get("/state")).json()
        assert state["state"] == "NAVIGATING" and state["calibrated"]

    serve(PhoneSource(), test)


def test_gyro_heading_source_reads_yaw():
    async def test(client, runtime):
        await client.post("/sensor", json=reading(10.0, 0, yaw_deg=250.0))
        await wait_for(lambda: runtime.nav.samples >= 1)
        assert runtime.nav.last.heading_deg == 250.0
        assert runtime.nav.last.raw["compass_deg"] == 10.0   # the raw message is kept for the log

    serve(PhoneSource("gyro"), test)


def test_routes_by_name_and_bad_routes():
    async def test(client, runtime):
        listing = await (await client.get("/routes")).json()
        assert "demo_L" in listing["routes"]
        loaded = await (await client.post("/route", json={"name": "demo_L"})).json()
        assert loaded == {"ok": True, "name": "demo_L", "waypoints": 3, "given": 3, "used": 3,
                          "dropped_other_floors": 0, "floors": 1}
        for bad in [{"name": "../config"}, {"name": "nope"}, {"waypoints": [[0, 0]]}, {"waypoints": "x"}]:
            assert (await client.post("/route", json=bad)).status == 409
        assert (await client.post("/start")).status == 409        # not calibrated

    serve(PhoneSource(), test)


def test_pages_and_sim_endpoint():
    async def test(client, runtime):
        page = await client.get("/phone")
        assert page.status == 200 and "Start sensors" in await page.text()
        assert (await client.post("/sim", json={"key": "step"})).status == 409   # not a sim run
        assert (await client.get("/cert")).status == 404                          # no TLS here

    serve(PhoneSource(), test)


def test_sim_source_is_driven_over_http():
    async def test(client, runtime):
        await client.post("/route", json=L_ROUTE)
        await wait_for(lambda: runtime.nav.samples >= 1)
        await client.post("/calibrate")
        await client.post("/start")
        assert (await client.post("/sim", json={"key": "right"})).status == 200
        assert (await client.post("/sim", json={"key": "step"})).status == 200
        assert (await client.post("/sim", json={"key": "jump"})).status == 400
        await wait_for(lambda: runtime.nav.last.step_count == 1)
        state = await (await client.get("/state")).json()
        assert state["sim"] and state["sensor"]["heading_deg"] == 15.0

    serve(SimSource(), test)


def test_wearer_height_over_http():
    async def test(client, runtime):
        answer = await client.post("/wearer", json={"height_cm": 160})
        body = await answer.json()
        assert answer.status == 200 and body["height_cm"] == 160
        assert abs(body["step_length_m"] - 0.7 * 160 / 175) < 1e-9
        state = await (await client.get("/state")).json()
        assert state["wearer"]["height_cm"] == 160.0
        assert abs(state["localizer"]["step_length_m"] - 0.64) < 1e-9
        for bad in ({"height_cm": 20}, {"height_cm": "tall"}, {}, [1]):
            refused = await client.post("/wearer", json=bad)
            assert refused.status == 409 and "centimetres" in (await refused.json())["error"]
        page = await (await client.get("/viewer")).text()
        assert 'id="height"' in page and "/wearer" in page

    serve(PhoneSource(), test)


def test_route_accepts_the_upstream_planners_answer():
    async def test(client, runtime):
        answer = {"route": {"nodes": ["a", "b", "c", "d"], "length": 9.0, "profile": "blind",
                            "points": [[0, 0, "f1"], [0, 4, "f1"], [5, 4, "f1"], [5, 4, "f2"]]},
                  "maneuvers": [{"say": "Walk 4 meters, then turn right"}]}
        loaded = await (await client.post("/route", json=answer)).json()
        assert loaded == {"ok": True, "name": None, "waypoints": 3, "given": 4, "used": 3,
                          "dropped_other_floors": 1, "floors": 1}      # one point on f2: nothing to walk there
        state = await (await client.get("/state")).json()
        assert state["route"]["waypoints"] == [[0.0, 0.0], [0.0, 4.0], [5.0, 4.0]]
        named = await (await client.post("/route", json={"name": "lobby to desk", "points": [[0, 0], [2, 0]]})).json()
        assert named["name"] == "lobby to desk" and named["waypoints"] == 2      # a name on a route is a label
        assert (await client.post("/route", json={"route": {"points": [[0, 0, "f1"]]}})).status == 409

    serve(PhoneSource(), test)
