"""HTTP and WebSocket front end.

    GET  /phone      sensor page for the iPhone        GET  /viewer   judges' view
    GET  /ws         sensor stream (WebSocket)         POST /sensor   same, one reading per request
    POST /route      {"waypoints": [[x, y], ...]}  or  {"name": "demo_L"}
                     or a planner's answer: {"route": {"points": [[x, y, floor], ...]}}
    POST /calibrate  POST /start  POST /stop
    POST /wearer     {"height_cm": 160}  scales the step length to the wearer's height
    GET  /state      everything the viewer draws       GET  /routes   names in routes/
    POST /sim        {"key": "left"|"right"|"step"}    (only with --sim)
    GET  /cert       the self-signed certificate       (only with --tls)
"""

from __future__ import annotations

import itertools
import json
import logging
import ssl
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .navigator import NavError
from .route import list_routes
from .runtime import Runtime
from .sensors.phone import BUSY

log = logging.getLogger("nudge.server")

WEB_DIR = Path(__file__).resolve().parent / "web"
RUNTIME = web.AppKey("runtime", Runtime)
CERT = web.AppKey("cert", object)
_connections = itertools.count(1)  # names each WebSocket connection


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except NavError as exc:
        return _json({"ok": False, "error": str(exc)}, status=409)
    except json.JSONDecodeError:
        return _json({"ok": False, "error": "request body is not valid JSON"}, status=400)


def _page(name: str):
    async def handler(request: web.Request) -> web.StreamResponse:
        path = WEB_DIR / name
        if not path.is_file():
            raise web.HTTPNotFound(text=f"{name} is not installed")
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    return handler


async def index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/viewer")


async def ws_sensor(request: web.Request) -> web.WebSocketResponse:
    runtime = request.app[RUNTIME]
    source = runtime.source
    ws = web.WebSocketResponse(heartbeat=5.0, max_msg_size=8192)
    await ws.prepare(request)
    feed = getattr(source, "feed", None)
    client = f"ws-{next(_connections)}"
    busy = False
    log.info("phone connected from %s", request.remote)
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not feed:
                continue
            feed(data, "ws", client)
            # Tell the page when it starts or stops being ignored for another phone.
            now_busy = source.last_status == BUSY
            if now_busy != busy:
                busy = now_busy
                if busy:
                    log.warning("ignoring a second phone at %s: another one is the sensor", request.remote)
                try:
                    await ws.send_json({"busy": busy})
                except (ConnectionError, RuntimeError):
                    break  # it hung up while we were answering
    finally:
        log.info("phone disconnected")
    return ws


async def post_sensor(request: web.Request) -> web.Response:
    source = request.app[RUNTIME].source
    feed = getattr(source, "feed", None)
    data = await request.json()
    ok = bool(feed and feed(data, "post", f"post-{request.remote}"))
    if ok:
        return _json({"ok": True})
    reason = getattr(source, "last_status", "invalid") if feed else "this run has no phone sensor"
    return _json({"ok": False, "error": reason}, status=409 if reason == BUSY else 400)


async def post_route(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME]
    data = await request.json()
    if isinstance(data, dict) and data.get("reroute") is True:
        # A new route for the walk that is under way: position and calibration are kept.
        info = runtime.reroute(data)
        return _json({"ok": True, "reroute": True, "name": runtime.nav.route_name,
                      "waypoints": len(runtime.nav.waypoints), **info})
    named = (isinstance(data, dict) and isinstance(data.get("name"), str)
             and not any(key in data for key in ("waypoints", "points", "route")))
    if named:
        runtime.load_route_named(data["name"])
    else:
        runtime.load_route(data)
    nav = runtime.nav
    return _json({"ok": True, "name": nav.route_name, "waypoints": len(nav.waypoints), **nav.route_info})


async def post_route_save(request: web.Request) -> web.Response:
    """{"name": "floor3_loop"}: keep the loaded route on the Pi under that name."""
    data = await request.json()
    name = data.get("name") if isinstance(data, dict) else None
    return _json({"ok": True, "name": request.app[RUNTIME].keep_route(name)})


async def get_routes(request: web.Request) -> web.Response:
    return _json({"routes": list_routes(request.app[RUNTIME].routes_dir)})


async def post_calibrate(request: web.Request) -> web.Response:
    return _json({"ok": True, **request.app[RUNTIME].calibrate()})


async def post_start(request: web.Request) -> web.Response:
    request.app[RUNTIME].start()
    return _json({"ok": True})


async def post_stop(request: web.Request) -> web.Response:
    request.app[RUNTIME].stop()
    return _json({"ok": True})


async def post_obstacle(request: web.Request) -> web.Response:
    """{"left_m": 2.4, "ahead_m": 0.9, "right_m": null}: the nearest thing in each direction ahead of the
    wearer, from whatever is looking (null: nothing in range). Send it several times a second."""
    data = await request.json()
    if not isinstance(data, dict) or "ahead_m" not in data:
        return _json({"ok": False, "error": 'send {"left_m": ..., "ahead_m": ..., "right_m": ...} in metres'}, status=400)
    info = request.app[RUNTIME].obstacle(data.get("left_m"), data["ahead_m"], data.get("right_m"))
    return _json({"ok": True, **info})


async def post_obstacle_mode(request: web.Request) -> web.Response:
    """{"enabled": true}: let obstacle readings stop and steer the wearer. Off by default."""
    data = await request.json()
    enabled = data.get("enabled") if isinstance(data, dict) else None
    return _json({"ok": True, "enabled": request.app[RUNTIME].obstacle_mode(enabled)})


async def post_floor(request: web.Request) -> web.Response:
    """The wearer is on the next floor, standing where its stretch of the route begins."""
    return _json({"ok": True, **request.app[RUNTIME].next_floor()})


async def post_mark(request: web.Request) -> web.Response:
    """{"waypoint": 2}: the wearer is standing on that waypoint now. No body: the next one."""
    data = await request.json() if request.can_read_body else None
    waypoint = data.get("waypoint") if isinstance(data, dict) else None
    return _json({"ok": True, **request.app[RUNTIME].mark(waypoint)})


async def post_wearer(request: web.Request) -> web.Response:
    """{"height_cm": 160}: scale the step length for whoever is wearing it now."""
    data = await request.json()
    height = data.get("height_cm") if isinstance(data, dict) else None
    step = request.app[RUNTIME].set_wearer_height(height)
    return _json({"ok": True, "height_cm": height, "step_length_m": step})


async def get_state(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME]
    state = runtime.nav.snapshot(runtime.source.now())
    state["source"] = runtime.source.name
    state["sim"] = runtime.source.name == "sim"
    state["transport"] = getattr(runtime.source, "transport", None)
    state["driver"] = type(runtime.driver).__name__
    return _json(state)


async def post_sim(request: web.Request) -> web.Response:
    source = request.app[RUNTIME].source
    if source.name != "sim":
        return _json({"ok": False, "error": "not running with --sim"}, status=409)
    data = await request.json()
    ok = isinstance(data, dict) and source.key(str(data.get("key")))
    return _json({"ok": bool(ok)}, status=200 if ok else 400)


async def get_cert(request: web.Request) -> web.StreamResponse:
    cert = request.app.get(CERT)
    if not cert:
        raise web.HTTPNotFound(text="not running with --tls")
    return web.FileResponse(cert, headers={"Content-Type": "application/x-x509-ca-cert",
                                           "Content-Disposition": 'attachment; filename="nudge.crt"'})


def build_app(runtime: Runtime, cert: Path | None = None) -> web.Application:
    app = web.Application(middlewares=[errors], client_max_size=1024 * 1024)
    app[RUNTIME] = runtime
    app[CERT] = cert
    app.add_routes([
        web.get("/", index),
        web.get("/phone", _page("phone.html")),
        web.get("/phone.html", _page("phone.html")),
        web.get("/viewer", _page("viewer.html")),
        web.get("/viewer.html", _page("viewer.html")),
        web.get("/ws", ws_sensor),
        web.post("/sensor", post_sensor),
        web.post("/route", post_route),
        web.get("/routes", get_routes),
        web.post("/route/save", post_route_save),
        web.post("/calibrate", post_calibrate),
        web.post("/start", post_start),
        web.post("/stop", post_stop),
        web.post("/wearer", post_wearer),
        web.post("/mark", post_mark),
        web.post("/floor", post_floor),
        web.post("/obstacle", post_obstacle),
        web.post("/obstacle/mode", post_obstacle_mode),
        web.get("/state", get_state),
        web.post("/sim", post_sim),
        web.get("/cert", get_cert),
    ])
    return app


async def serve(app: web.Application, host: str, port: int,
                tls: tuple[int, ssl.SSLContext] | None = None) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    if tls:
        await web.TCPSite(runner, host, tls[0], ssl_context=tls[1]).start()
    return runner
