"""Structured run evidence.

One directory per run holding a JSONL event log, the snapshots the model was
shown, and screenshots. Events are append-only and redacted on the way in.

The log is written to be read by a person debugging a failure at some later
date, so every event says what was attempted, against what, and what came back -
not merely that a step happened.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.safety.redaction import Redactor


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


class RunLog:
    def __init__(self, root: str | Path, run_id: str,
                 redactor: Redactor | None = None) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)
        self.redactor = redactor or Redactor()
        self._path = self.dir / "log.jsonl"
        self._started = time.monotonic()

    # -------------------------------------------------------------- writing

    def event(self, kind: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
            "kind": kind,
            **self.redactor.scrub(fields),
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    def snapshot(self, index: int, rendered: str) -> Path:
        path = self.dir / "snapshots" / f"{index:02d}.txt"
        path.write_text(self.redactor.text(rendered), encoding="utf-8")
        return path

    def screenshot_path(self, index: int, label: str = "") -> Path:
        suffix = f"-{label}" if label else ""
        return self.dir / "screenshots" / f"{index:02d}{suffix}.png"

    def write(self, name: str, payload: Any) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(self.redactor.text(payload), encoding="utf-8")
        else:
            path.write_text(
                json.dumps(self.redactor.scrub(payload), indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        return path

    # -------------------------------------------------------------- reading

    def events(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(line) for line in
                self._path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def relative(self, path: Path) -> str:
        return str(path).replace("\\", "/")
