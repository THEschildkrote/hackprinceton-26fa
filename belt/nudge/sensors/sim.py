"""Simulated sensors: keyboard-driven or scripted.

Script format, one command per line, `#` starts a comment:

    heading DEG            set the compass reading
    offset DEG             add a constant compass error from here on
    turn DEG [SECONDS]     rotate on the spot, positive = right (default 1 s)
    step N [CADENCE]       walk N steps, one every CADENCE seconds (default 0.55)
    arc DEG N [CADENCE]    walk N steps while turning DEG in total
    wait SECONDS           stand still
    gap SECONDS            sensor dropout: no samples at all
    cmd calibrate|start|stop|next_floor   issue a runtime command at this point
    quit                   end the run

`script_events` turns a script into a deterministic event list with
synthetic timestamps; SimSource plays the same events in real time.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import AsyncIterator, Callable, Iterator

from ..geometry import wrap360
from .base import SensorSample, SensorSource

DEFAULT_CADENCE_S = 0.55
COMMANDS = ("calibrate", "start", "stop", "next_floor")

# op -> (min args, max args)
_ARITY = {"heading": (1, 1), "offset": (1, 1), "turn": (1, 2), "step": (1, 2), "arc": (2, 3),
          "wait": (1, 1), "gap": (1, 1), "cmd": (1, 1), "quit": (0, 0)}


class ScriptError(ValueError):
    pass


def parse_script(text: str) -> list[tuple[str, list]]:
    ops: list[tuple[str, list]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        tokens = line.split("#", 1)[0].split()
        if not tokens:
            continue
        op, args = tokens[0].lower(), tokens[1:]
        if op not in _ARITY:
            raise ScriptError(f"line {lineno}: unknown command {op!r}")
        lo, hi = _ARITY[op]
        if not lo <= len(args) <= hi:
            raise ScriptError(f"line {lineno}: {op} takes {lo}" + (f"-{hi}" if hi != lo else "") + " argument(s)")
        if op == "cmd":
            if args[0] not in COMMANDS:
                raise ScriptError(f"line {lineno}: cmd must be one of {', '.join(COMMANDS)}")
            ops.append((op, [args[0]]))
            continue
        try:
            values = [float(a) for a in args]
        except ValueError:
            raise ScriptError(f"line {lineno}: {op} needs numbers") from None
        if op in ("step", "arc"):
            idx = 0 if op == "step" else 1
            if values[idx] != int(values[idx]) or values[idx] < 0:
                raise ScriptError(f"line {lineno}: step count must be a whole number")
            values[idx] = int(values[idx])
        ops.append((op, values))
    return ops


def script_events(text: str, dt: float = 0.05) -> Iterator[tuple]:
    """Yield ("sample", SensorSample), ("cmd", name, t), ("gap", t0, t1), ("quit", t)."""
    heading = 0.0
    offset = 0.0
    steps = 0
    k = 0  # tick counter; time is k * dt
    emitted = False

    def now() -> float:
        return round(k * dt, 6)

    def ticks(seconds: float) -> int:
        return max(1, round(seconds / dt))

    def sample() -> tuple:
        nonlocal emitted
        emitted = True
        return ("sample", SensorSample(now(), wrap360(heading + offset), steps))

    for op, args in parse_script(text):
        if op == "heading":
            heading = args[0]
        elif op == "offset":
            offset = args[0]
        elif op == "turn":
            seconds = args[1] if len(args) > 1 else 1.0
            n = ticks(seconds) if seconds > 0 else 0
            if n == 0:
                heading += args[0]
            for _ in range(n):
                k += 1
                heading += args[0] / n
                yield sample()
        elif op in ("step", "arc"):
            turn = args[0] if op == "arc" else 0.0
            count = args[1] if op == "arc" else args[0]
            rest = args[2:] if op == "arc" else args[1:]
            n = ticks(rest[0] if rest else DEFAULT_CADENCE_S)
            for _ in range(count):
                for i in range(n):
                    k += 1
                    if count:
                        heading += turn / (count * n)
                    if i == n - 1:
                        steps += 1
                    yield sample()
        elif op == "wait":
            for _ in range(ticks(args[0])):
                k += 1
                yield sample()
        elif op == "gap":
            start = now()
            k += ticks(args[0])
            yield ("gap", start, now())
        elif op == "cmd":
            if not emitted:  # a command needs at least one reading to act on
                k += 1
                yield sample()
            yield ("cmd", args[0], now())
        elif op == "quit":
            yield ("quit", now())
            return


def drive(nav, events, stale_after_s: float | None = None) -> list:
    """Feed scripted events straight into a Navigator, with no clock or
    event loop. Returns every action the navigator issued, in order."""
    stale = nav.cfg.sensors.stale_after_s if stale_after_s is None else stale_after_s
    issued: list = []
    for event in events:
        kind = event[0]
        if kind == "sample":
            issued += nav.on_sample(event[1])
        elif kind == "cmd":
            if event[1] == "stop":
                issued += nav.stop(event[2])
            else:
                getattr(nav, event[1])(event[2])
        elif kind == "gap":
            if event[2] - event[1] > stale:
                issued += nav.on_stale(round(event[1] + stale, 6))
        elif kind == "quit":
            break
    return issued


class SimSource(SensorSource):
    """Heading and steps from the keyboard, the viewer page, or a script."""

    name = "sim"

    def __init__(self, script: str | None = None, rate_hz: float = 20.0, speed: float = 1.0,
                 turn_step_deg: float = 15.0) -> None:
        self.script = script
        self.rate_hz = rate_hz
        self.speed = speed
        self.turn_step_deg = turn_step_deg
        self.heading = 0.0
        self.steps = 0
        self.on_command: Callable[[str], None] | None = None
        self._wake = asyncio.Event()
        self._quit = False
        self._t_anchor = 0.0
        self._wall_anchor = time.monotonic()

    def now(self) -> float:
        return self._t_anchor + (time.monotonic() - self._wall_anchor) * self.speed

    def _anchor(self, t: float) -> None:
        self._t_anchor = t
        self._wall_anchor = time.monotonic()

    # -- controls (call from the event loop) -------------------------------

    def turn(self, deg: float) -> None:
        self.heading = wrap360(self.heading + deg)
        self._wake.set()

    def step(self, n: int = 1) -> None:
        self.steps += n
        self._wake.set()

    def key(self, name: str) -> bool:
        if name == "left":
            self.turn(-self.turn_step_deg)
        elif name == "right":
            self.turn(self.turn_step_deg)
        elif name == "step":
            self.step()
        else:
            return False
        return True

    def quit(self) -> None:
        self._quit = True
        self._wake.set()

    # -- the sample stream -------------------------------------------------

    async def samples(self) -> AsyncIterator[SensorSample]:
        if self.script is not None:
            base = self.now()
            last = 0.0
            for event in script_events(self.script, dt=1.0 / self.rate_hz):
                kind = event[0]
                if kind == "sample":
                    s = event[1]
                    await asyncio.sleep((s.t - last) / self.speed)
                    last = s.t
                    self.heading, self.steps = s.heading_deg, s.step_count
                    self._anchor(base + s.t)
                    yield SensorSample(round(base + s.t, 6), s.heading_deg, s.step_count)
                elif kind == "gap":
                    await asyncio.sleep((event[2] - last) / self.speed)
                    last = event[2]
                    self._anchor(base + last)
                elif kind == "cmd":
                    if self.on_command:
                        self.on_command(event[1])
                elif kind == "quit":
                    return
        while not self._quit:
            yield SensorSample(round(self.now(), 6), self.heading, self.steps)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0 / (self.rate_hz * self.speed))
            except asyncio.TimeoutError:
                pass


class Keyboard:
    """Single-key input on a background thread, delivered on the event loop.

    Keys are reported as "left", "right", "up", "down", "space" or the
    character itself. Does nothing when stdin is not a terminal.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, callback: Callable[[str], None]) -> None:
        self._loop = loop
        self._callback = callback
        self._stop = threading.Event()
        self._restore: Callable[[], None] | None = None

    def start(self) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        target = self._run_windows if sys.platform == "win32" else self._run_posix
        threading.Thread(target=target, name="nudge-keys", daemon=True).start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._restore:
            self._restore()
            self._restore = None

    def _emit(self, key: str) -> None:
        try:
            self._loop.call_soon_threadsafe(self._callback, key)
        except RuntimeError:  # loop already closed
            self._stop.set()

    def _run_windows(self) -> None:
        import msvcrt

        arrows = {"K": "left", "M": "right", "H": "up", "P": "down"}
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                key = arrows.get(msvcrt.getwch())
                if key:
                    self._emit(key)
            elif ch == "\x03":
                self._emit("q")
            elif ch == " ":
                self._emit("space")
            else:
                self._emit(ch.lower())

    def _run_posix(self) -> None:
        import os
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self._restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        tty.setcbreak(fd)
        try:
            while not self._stop.is_set():
                if not select.select([fd], [], [], 0.05)[0]:
                    continue
                # Read the descriptor itself. sys.stdin.read() would pull the
                # whole of an arrow key's escape sequence into Python's own
                # buffer, where select() cannot see the rest of it.
                data = os.read(fd, 64)
                if not data:
                    return  # the terminal went away
                if _unfinished_escape(data) and select.select([fd], [], [], 0.03)[0]:
                    data += os.read(fd, 64)
                for key in decode_keys(data):
                    self._emit(key)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


_ARROWS = {ord("A"): "up", ord("B"): "down", ord("C"): "right", ord("D"): "left"}


def _unfinished_escape(data: bytes) -> bool:
    """Does the data end part-way through an escape sequence?"""
    tail = data[data.rfind(b"\x1b"):] if b"\x1b" in data else b""
    if not tail:
        return False
    if len(tail) == 1:
        return True  # may be a bare Escape; a short wait tells
    if tail[1] not in b"[O":
        return False
    return len(tail) == 2 or not 0x40 <= tail[-1] <= 0x7E


def decode_keys(data: bytes) -> list[str]:
    """Key names from raw terminal input: arrows arrive as ESC [ A..D (or
    ESC O A..D), possibly with parameters; everything else is one byte."""
    keys: list[str] = []
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        i += 1
        if byte == 0x1B:
            if i < n and data[i] in b"[O":
                i += 1
                while i < n and 0x20 <= data[i] <= 0x3F:  # parameters, e.g. the "1;5" in ESC [ 1 ; 5 C
                    i += 1
                if i < n:
                    key = _ARROWS.get(data[i])
                    i += 1
                    if key:
                        keys.append(key)
            continue  # a bare Escape, or a sequence that is not an arrow
        if byte == 0x20:
            keys.append("space")
        elif byte == 0x03:
            keys.append("q")
        elif 0x21 <= byte <= 0x7E:
            keys.append(chr(byte).lower())
    return keys
