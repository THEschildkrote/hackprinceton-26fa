"""Command line entry point.

    nudge [--sim] [options]  run the on-body runtime
    nudge taps [--mock]      exercise every tap primitive
    nudge replay LOG         replay a JSONL walk log and check it reproduces

Run `nudge --help`, `nudge taps --help` or `nudge replay --help` for the options.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import subprocess
import sys
import time

from pathlib import Path

from .actuators import SIDES, DriverUnavailable, MockDriver, TapDriver, make_driver
from .config import ConfigError, load_config
from .eventlog import EventLog, new_log_path
from .navigator import Navigator, NavError
from .replay import replay_file
from .route import RouteError, read_route_file
from .runtime import Runtime
from .sensors.imu import ImuSource
from .sensors.phone import PhoneSource
from .sensors.sim import Keyboard, ScriptError, SimSource
from .tls import TlsError, ensure_certificate, local_addresses, server_context

log = logging.getLogger("nudge")

THROTTLE_BITS = {
    0: "under-voltage now",
    1: "ARM frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "ARM frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


def read_throttled() -> str:
    """`vcgencmd get_throttled`, decoded. Only meaningful on the Pi."""
    if not shutil.which("vcgencmd"):
        return "vcgencmd not found (not a Raspberry Pi?)"
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        value = int(out.split("=")[1], 16)
    except Exception as exc:
        return f"vcgencmd get_throttled failed: {exc}"
    flags = [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]
    return f"{out}  ({'; '.join(flags) if flags else 'no under-voltage or throttling'})"


# -- nudge taps --------------------------------------------------------------


async def _taps_demo(driver: TapDriver, pause: float) -> None:
    steps = [("tap", "left"), ("tap", "right"), ("double_tap", "left"), ("double_tap", "right"),
             ("stop", None), ("arrived", None)]
    # Stay clear of the driver's own rate limit so no pattern is dropped.
    pause = max(pause, driver.cfg.min_interval_s)
    for kind, side in steps:
        if not await driver.do(kind, side):
            print(f"  ({kind} {side or ''} was dropped by the rate limit)")
        await asyncio.sleep(pause)


async def _taps_soak(driver: TapDriver, seconds: float) -> None:
    """One steering tap a second, alternating sides, like a walk that needs
    constant correction; every tenth beat is a stop() so both servos fire."""
    print(f"before: {read_throttled()}")
    start = time.monotonic()
    beat = 0
    while time.monotonic() - start < seconds:
        tick = time.monotonic()
        if beat % 10 == 9:
            await driver.stop()
        else:
            await driver.tap(SIDES[beat % 2])
        beat += 1
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    print(f"{beat} patterns in {time.monotonic() - start:.0f} s")
    print(f"after:  {read_throttled()}")


async def _taps(args: argparse.Namespace) -> int:
    cfg, _ = load_config(args.config)
    if args.swing is not None:
        # Try a different tap depth without editing config.toml: each arm
        # swings this far from rest, in the direction its contact angle is set.
        for name, pod in (("left", cfg.actuators.left), ("right", cfg.actuators.right)):
            direction = 1.0 if pod.contact_deg >= pod.rest_deg else -1.0
            pod.contact_deg = pod.rest_deg + direction * args.swing
            print(f"{name} pod: rest {pod.rest_deg:.0f} deg, contact {pod.contact_deg:.0f} deg")
    driver = make_driver(cfg.actuators, "mock" if args.mock else None)
    try:
        if args.rest:
            await driver.rest()
            print("both pods at rest")
        elif args.jog:
            side, angle = args.jog[0], float(args.jog[1])
            used = await driver.jog(side, angle)
            print(f"{side} pod held at {used:.0f} deg. Enter = back to rest.")
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        elif args.only:
            await driver.do(args.only, args.side if args.only in ("tap", "double_tap") else None)
            await asyncio.sleep(0.2)
        elif args.soak:
            await _taps_soak(driver, args.soak)
        else:
            await _taps_demo(driver, args.pause)
    finally:
        await driver.close()
    return 0


def taps_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="nudge taps", description="Exercise the shoulder pods.")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--mock", action="store_true", help="log the taps instead of moving servos")
    p.add_argument("--only", choices=["tap", "double_tap", "stop", "arrived"], help="play one pattern")
    p.add_argument("--side", choices=SIDES, default="left", help="side for --only tap/double_tap")
    p.add_argument("--jog", nargs=2, metavar=("SIDE", "DEG"), help="hold one arm at an angle")
    p.add_argument("--swing", type=float, metavar="DEG",
                   help="for this run, swing each arm this many degrees from rest (overrides contact_deg)")
    p.add_argument("--soak", type=float, metavar="SECONDS",
                   help="tap once a second for this long, then report vcgencmd get_throttled")
    p.add_argument("--pause", type=float, default=0.8, help="gap between patterns in the demo")
    p.add_argument("--rest", action="store_true", help="just return both arms to rest")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.jog and args.jog[0] not in SIDES:
        p.error("--jog SIDE must be left or right")
    setup_logging(args.verbose)
    return asyncio.run(_taps(args))


# -- nudge [run] -------------------------------------------------------------

GO_DELAY_S = 3.0  # --go waits for this much steady sensor data before calibrating
SIM_KEYS = "left/right arrows turn 15 deg, space = one step, c = calibrate, s = start, x = stop, q = quit"


def build_source(args: argparse.Namespace, cfg):
    if args.sim or cfg.sensors.source == "sim":
        script = Path(args.script).read_text(encoding="utf-8") if args.script else None
        return SimSource(script=script, speed=args.speed)
    if cfg.sensors.source == "imu":
        return ImuSource()
    return PhoneSource(cfg.sensors.heading_source, takeover_s=cfg.sensors.stale_after_s,
                       max_age_s=cfg.sensors.stale_after_s)


async def start_server(args: argparse.Namespace, cfg, base: Path, runtime: Runtime):
    # Imported here, not at the top: aiohttp is only needed to serve pages, so
    # `nudge taps` and `nudge --sim --no-server` run with nothing installed.
    try:
        from .server import build_app, serve
    except ImportError as exc:
        raise DriverUnavailable(f"the web server needs aiohttp ({exc}): pip install -e . "
                                "or run with --sim --no-server") from None
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    tls = None
    cert = None
    if args.tls or args.tls_cert:
        if args.tls_cert:
            cert, key = Path(args.tls_cert), Path(args.tls_key)
        else:
            cert, key = ensure_certificate(base / cfg.server.cert_dir)
        tls = (cfg.server.tls_port, server_context(cert, key))
    runner = await serve(build_app(runtime, cert), host, port, tls)
    names = local_addresses() or ["localhost"]
    for name in names:
        log.info("viewer  http://%s:%d/viewer", name, port)
    if tls:
        for name in names:
            log.info("phone   https://%s:%d/phone   (self-signed: accept the warning once)", name, tls[0])
    else:
        log.info("phone   needs HTTPS: run with --tls, or point an HTTPS tunnel at port %d and open /phone", port)
    return runner


async def _run(args: argparse.Namespace) -> int:
    cfg, base = load_config(args.config)
    source = build_source(args, cfg)
    sim = isinstance(source, SimSource)
    driver_kind = "mock" if args.mock or (sim and not args.servo) else cfg.actuators.driver
    if driver_kind == "mock" and source.speed != 1.0:
        # A sped-up script needs the mock taps sped up to match.
        speed = source.speed
        driver = MockDriver(cfg.actuators, sleep=lambda d: asyncio.sleep(d / speed),
                            clock=lambda: time.monotonic() * speed)
    else:
        driver = make_driver(cfg.actuators, driver_kind)
    log_path = None if args.no_log else new_log_path(base / cfg.logging.dir)
    events = EventLog(log_path)
    nav = Navigator(cfg, events)
    runtime = Runtime(cfg, nav, source, driver, base / cfg.route.dir,
                      autostart=args.go or (sim and not args.script),
                      autostart_delay_s=0.0 if sim else GO_DELAY_S)

    route = args.route or cfg.route.default
    if route:
        if Path(route).is_file() or (base / route).is_file():
            path = Path(route) if Path(route).is_file() else base / route
            runtime.load_route(read_route_file(path), name=path.stem)
        else:
            runtime.load_route_named(route)
    if log_path:
        log.info("logging to %s", log_path)

    loop = asyncio.get_running_loop()
    keyboard = None
    if sim:
        def on_key(key: str) -> None:
            if key in ("left", "right"):
                source.key(key)
            elif key in ("space", "up"):
                source.key("step")
            elif key in ("c", "s", "x"):
                runtime.command({"c": "calibrate", "s": "start", "x": "stop"}[key])
            elif key == "q":
                source.quit()

        keyboard = Keyboard(loop, on_key)
        if keyboard.start():
            print(f"sim keys: {SIM_KEYS}")
    # SIGTERM is how systemd stops the service. It ends the run the same way
    # the end of a sim script does: arms to rest, log closed, exit status 0.
    task = asyncio.current_task()
    terminated = False

    def on_sigterm() -> None:
        nonlocal terminated
        terminated = True
        task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, on_sigterm)
    except (NotImplementedError, AttributeError):
        pass  # Windows: Ctrl+C still works

    runner = None
    try:
        if not args.no_server:
            runner = await start_server(args, cfg, base, runtime)
        await runtime.run()
    except asyncio.CancelledError:
        if not terminated:
            raise
        task.uncancel()  # the cancellation was ours: finish normally
        log.info("SIGTERM: shutting down")
    finally:
        if keyboard:
            keyboard.stop()
        if runner:
            await runner.cleanup()
        await source.close()
        await driver.close()
        events.close()
    return 0


def parse_run_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="nudge", description="Nudge on-body runtime. See also: nudge taps --help")
    p.add_argument("--config", help="path to config.toml (default: ./config.toml)")
    p.add_argument("--sim", action="store_true", help="simulated sensors (keyboard or --script) and mock taps")
    p.add_argument("--script", metavar="FILE", help="with --sim: play a scripted walk")
    p.add_argument("--speed", type=float, default=1.0, help="with --script: time multiplier")
    p.add_argument("--mock", action="store_true", help="log taps instead of moving servos")
    p.add_argument("--servo", action="store_true", help="with --sim: drive the real servos anyway")
    p.add_argument("--route", help="route name in routes/ or a path to a route JSON file")
    p.add_argument("--go", action="store_true",
                   help="no operator: calibrate and start once sensor data has flowed for 3 s")
    p.add_argument("--no-log", action="store_true", help="do not write a JSONL walk log")
    p.add_argument("--tls", action="store_true",
                   help="also serve HTTPS with a self-signed certificate (created in certs/ on first use)")
    p.add_argument("--tls-cert", metavar="FILE", help="serve HTTPS with this certificate instead")
    p.add_argument("--tls-key", metavar="FILE", help="private key for --tls-cert")
    p.add_argument("--host", help="address to listen on (default from config: 0.0.0.0)")
    p.add_argument("--port", type=int, help="HTTP port (default from config: 8000)")
    p.add_argument("--no-server", action="store_true", help="with --sim: no web server, keyboard only")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.script and not args.sim:
        p.error("--script needs --sim")
    if args.speed <= 0:
        p.error("--speed must be positive")
    if bool(args.tls_cert) != bool(args.tls_key):
        p.error("--tls-cert and --tls-key go together")
    if args.no_server and not args.sim:
        p.error("--no-server only makes sense with --sim")
    return args


def run_main(argv: list[str]) -> int:
    args = parse_run_args(argv)
    setup_logging(args.verbose)
    return asyncio.run(_run(args))


# -- nudge replay ------------------------------------------------------------


def replay_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="nudge replay", description="Replay a JSONL walk log.")
    p.add_argument("log", help="a logs/walk-*.jsonl file")
    p.add_argument("--config", help="replay with this config instead of the one stored in the log (what-if)")
    p.add_argument("--taps", action="store_true", help="list the taps the replay produced")
    args = p.parse_args(argv)
    cfg = load_config(args.config)[0] if args.config else None
    result = replay_file(args.log, cfg)
    print(result.summary())
    if args.taps:
        for t, kind, side in result.replayed_taps:
            print(f"  {t:8.2f} s  {kind} {side or ''}")
    if not result.used_logged_config:
        print("what-if run with a different config: differences from the log are the point")
        return 0
    print("REPLAY OK: same taps, poses and corrections as logged" if result.ok else "REPLAY MISMATCH")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "taps":
            return taps_main(argv[1:])
        if argv and argv[0] == "replay":
            return replay_main(argv[1:])
        if argv and argv[0] == "run":
            argv = argv[1:]
        return run_main(argv)
    except (ConfigError, DriverUnavailable, NavError, RouteError, ScriptError, TlsError, OSError) as exc:
        print(f"nudge: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
