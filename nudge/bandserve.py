"""the collar, bound to one navigator session at a time.

the pi has one band and the server can hold many sessions, so this attaches it to whichever
one is being walked. it stays off until something attaches it: the server also runs on
laptops with no gpio, where importing gpiozero fails outright.

the ticker thread is the whole point. positions reach the server as http posts at walking
pace, but a stroke only renders if the servo is commanded every few milliseconds, so the
band cannot be driven from the request handlers alone. sync() tells the engine what
changed and the thread renders it.

attach from the debug view, or set NUDGE_BAND=1 before starting the server and the collar
follows whatever walk begins (NUDGE_BAND=dry to wire everything up without touching gpio).
"""
import threading
import time

from .band import NullBand, ServoBand
from .bandrun import BandRunner

TICK_S = 0.005


class BandService:
    """one band, one session, one ticker. every method is safe to call from any thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._runner = None
        self._session = None
        self._thread = None
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self.mode = None      # "servo", "dry", or None when nothing is attached
        self.error = None

    # --- lifecycle ----------------------------------------------------------

    def attach(self, session, nav, dry=False, left_pin=12, right_pin=13):
        """bind the collar to a session. falls back to dry if the gpio isn't there."""
        with self._lock:
            self._teardown()
            self.error = None
            band, mode = NullBand(), "dry"
            if not dry:
                try:
                    band, mode = ServoBand(left_pin=left_pin, right_pin=right_pin), "servo"
                except Exception as ex:
                    # no gpiozero, no pi, pins busy: say so in the status and carry on
                    self.error = f"{type(ex).__name__}: {ex}"
            self._runner = BandRunner(nav, band=band)
            self._session, self.mode = session, mode
            self._t0 = time.monotonic()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._tick_forever, args=(self._stop,),
                                            name="nudge-band", daemon=True)
            self._thread.start()
            self._runner.sync(None, self._now())
            return self.status()

    def detach(self):
        with self._lock:
            self._teardown()
            return self.status()

    def _teardown(self):
        """called holding the lock. never join the ticker here - it needs the lock to exit."""
        self._stop.set()
        self._thread = None
        runner, self._runner = self._runner, None
        self._session, self.mode = None, None
        if runner is not None:
            try:
                runner.close()
            except Exception as ex:
                self.error = f"{type(ex).__name__}: {ex}"

    # --- the loop -----------------------------------------------------------

    def _now(self):
        return int((time.monotonic() - self._t0) * 1000)

    def _tick_forever(self, stop):
        while not stop.is_set():
            try:
                with self._lock:
                    if self._runner is not None:
                        self._runner.tick(self._now())
            except Exception as ex:
                self.error = f"{type(ex).__name__}: {ex}"
            stop.wait(TICK_S)

    # --- from the request handlers ------------------------------------------

    def sync(self, session, nav, heading=None):
        """the caller already drove the navigator; tell the engine what changed."""
        with self._lock:
            if self._runner is None or session != self._session:
                return False
            if nav is not self._runner.nav:
                self._runner.nav = nav  # the session rebuilt its navigator
            self._runner.sync(heading, self._now())
            return True

    def status(self):
        with self._lock:
            return {"attached": self._runner is not None, "session": self._session,
                    "mode": self.mode, "busy": bool(self._runner and self._runner.busy),
                    "error": self.error}


SERVICE = BandService()


def attach(session, nav, dry=False, **kw):
    return SERVICE.attach(session, nav, dry=dry, **kw)


def sync(session, nav, heading=None):
    return SERVICE.sync(session, nav, heading)


def detach():
    return SERVICE.detach()


def status():
    return SERVICE.status()
