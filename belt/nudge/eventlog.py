"""JSONL event log: one JSON object per line, in the order things happened.

Record types
  inputs   meta, route, calibrate, start, stop, sensor, stale, wearer, mark, reroute, floor, obstacle,
           obstacle_mode
  outputs  pose, tap, and the localizer's snap/calibration events

Replaying the inputs through a fresh Navigator must reproduce the outputs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

INPUT_TYPES = ("meta", "route", "calibrate", "start", "stop", "sensor", "stale", "wearer", "mark", "reroute", "floor",
               "obstacle", "obstacle_mode")


class EventLog:
    """Writes to a file when given a path; always usable as a no-op sink."""

    def __init__(self, path: Path | None = None, keep: bool = False) -> None:
        self.path = path
        self.records: list[dict[str, Any]] | None = [] if keep else None
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8", newline="\n", buffering=1)

    def write(self, type: str, **fields: Any) -> None:
        record = {"type": type, **fields}
        if self.records is not None:
            self.records.append(record)
        if self._fh is not None:
            self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def new_log_path(directory: Path) -> Path:
    return directory / f"walk-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def read_log(path: Path) -> list[dict[str, Any]]:
    records = []
    with Path(path).open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: not valid JSON ({exc})") from None
    return records
