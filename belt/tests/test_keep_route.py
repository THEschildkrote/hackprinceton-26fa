"""Keeping a route on the belt: once it is a file on the Pi, a walk needs no server and no laptop."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nudge.actuators import MockDriver
from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.navigator import Navigator
from nudge.route import RouteError, list_routes, save_route_file
from nudge.runtime import Runtime
from nudge.sensors.sim import SimSource
from nudge.server import build_app

from test_server import wait_for

LOOP = {"route": {"points": [[0, 0, "f3"], [0, 24, "f3"], [18, 24, "f3"], [18, 0, "f3"], [0, 0, "f3"]]}}
TWO_FLOORS = {"points": [[0, 0, "f1"], [0, 4, "f1"], [0, 4, "f2"], [5, 4, "f2"]]}


def serve(routes_dir, test):
    async def main():
        cfg = Config.from_dict({})
        runtime = Runtime(cfg, Navigator(cfg, EventLog(keep=True)), SimSource(), MockDriver(cfg.actuators), routes_dir)
        loop_task = asyncio.create_task(runtime.run())
        client = TestClient(TestServer(build_app(runtime)))
        await client.start_server()
        try:
            return await test(client, runtime)
        finally:
            await client.close()
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)

    return asyncio.run(main())


def test_a_route_sent_once_is_kept_and_loads_by_name_from_then_on(tmp_path):
    async def test(client, runtime):
        assert (await client.post("/route/save", json={"name": "x"})).status == 409        # nothing loaded yet
        await client.post("/route", json=LOOP)                                              # as the planner sends it
        kept = await (await client.post("/route/save", json={"name": "floor3_loop"})).json()
        assert kept == {"ok": True, "name": "floor3_loop"}
        assert (await (await client.get("/routes")).json())["routes"] == ["floor3_loop"]
        assert (await (await client.get("/state")).json())["route"]["name"] == "floor3_loop"
        await client.post("/route", json={"waypoints": [[0, 0], [1, 0]]})                   # something else is loaded
        loaded = await (await client.post("/route", json={"name": "floor3_loop"})).json()   # and the loop comes back
        assert loaded["ok"] and loaded["name"] == "floor3_loop" and loaded["waypoints"] == 5
        state = await (await client.get("/state")).json()
        assert state["route"]["waypoints"] == [[0.0, 0.0], [0.0, 24.0], [18.0, 24.0], [18.0, 0.0], [0.0, 0.0]]
        page = await (await client.get("/viewer")).text()
        assert 'id="keep"' in page and "/route/save" in page

    serve(tmp_path, test)
    on_disk = json.loads((tmp_path / "floor3_loop.json").read_text(encoding="utf-8"))
    assert on_disk["name"] == "floor3_loop" and on_disk["waypoints"][1] == [0.0, 24.0]     # this runtime's own format
    assert not list(tmp_path.glob("*.tmp"))


def test_a_kept_route_is_still_there_for_a_runtime_started_afterwards(tmp_path):
    async def first(client, runtime):
        await client.post("/route", json=TWO_FLOORS)
        assert (await client.post("/route/save", json={"name": "upstairs"})).status == 200

    async def later(client, runtime):
        loaded = await (await client.post("/route", json={"name": "upstairs"})).json()
        assert loaded["floors"] == 2 and loaded["waypoints"] == 2                           # both floors were kept
        await wait_for(lambda: runtime.nav.samples >= 1)
        assert (await client.post("/calibrate")).status == 200                              # and it walks: no server asked

    serve(tmp_path, first)
    serve(tmp_path, later)
    assert json.loads((tmp_path / "upstairs.json").read_text(encoding="utf-8"))["points"][-1] == [5.0, 4.0, "f2"]


def test_names_that_are_refused(tmp_path):
    points = [[0.0, 0.0, None], [0.0, 5.0, None]]
    for bad in ("../etc", "my route", "", None, 7, "demo_L", "Demo2"):
        with pytest.raises(RouteError):
            save_route_file(tmp_path, bad, points)
    with pytest.raises(RouteError, match="no route"):
        save_route_file(tmp_path, "empty", [])
    assert list_routes(tmp_path) == []
    save_route_file(tmp_path, "hall-1", points)
    save_route_file(tmp_path, "hall-1", points + [[3.0, 5.0, None]])                        # keeping again replaces it
    assert list_routes(tmp_path) == ["hall-1"]
    assert len(json.loads((tmp_path / "hall-1.json").read_text(encoding="utf-8"))["waypoints"]) == 3
