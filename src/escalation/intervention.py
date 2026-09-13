"""The request for help, and everything a person needs to act on it.

An intervention is useless if answering it requires going and finding out what
was happening. It carries what the run was doing, where it stopped, why, what
the screen looked like at that moment, and the tail of the log - all redacted,
because an operator queue is one more place regulated data should not collect.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TAIL = 12


@dataclass
class Intervention:
    id: str
    run_id: str
    capability_id: str
    capability_version: str
    step_id: str
    code: str
    detail: str
    url: str
    inputs_digest: str
    created_at: str
    step_note: str = ""
    step_risk: str = "safe"
    screenshot: str = ""
    snapshot: str = ""
    log_tail: list[dict] = field(default_factory=list)
    options: list[str] = field(default_factory=lambda: [
        "resume --approve    automation performs the step",
        "resume --completed  the step was done by hand; automation carries on after it",
        "abort               end the run here",
    ])

    @classmethod
    def build(cls, *, run_id: str, capability, step_id: str, code: str,
              detail: str, inputs_digest: str, surface, log) -> Intervention:
        try:
            step = capability.step(step_id)
            note, risk = step.note, step.risk
        except StopIteration:
            note, risk = "", "safe"

        created = datetime.now(timezone.utc)
        intervention = cls(
            id=f"int-{created.strftime('%Y%m%dT%H%M%SZ')}",
            run_id=run_id,
            capability_id=capability.id,
            capability_version=capability.version,
            step_id=step_id, code=code, detail=detail,
            url=_safely(surface.url, ""),
            inputs_digest=inputs_digest,
            created_at=created.isoformat(timespec="milliseconds"),
            step_note=note, step_risk=risk,
        )

        # Captured now, while the screen is still the one being asked about.
        try:
            intervention.screenshot = log.relative(
                surface.screenshot(log.dir / "intervention" / f"{step_id}.png"))
        except Exception:
            pass
        try:
            from src.surface.snapshot import render
            intervention.snapshot = log.relative(
                log.write(f"intervention/{step_id}.txt", render(surface.snapshot())))
        except Exception:
            pass

        intervention.log_tail = log.events()[-TAIL:]
        return intervention

    def write(self, directory: str | Path) -> Path:
        path = Path(directory) / "intervention.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, default=str) + "\n",
                        encoding="utf-8")
        return path

    @classmethod
    def read(cls, directory: str | Path) -> Intervention | None:
        path = Path(directory) / "intervention.json"
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def describe(self) -> str:
        lines = [
            f"{self.id}   {self.code}",
            f"  capability  {self.capability_id}@{self.capability_version}",
            f"  stopped at  {self.step_id}"
            + (f"  ({self.step_risk})" if self.step_risk != "safe" else ""),
            f"  why         {self.detail}",
            f"  url         {self.url}",
            f"  inputs      {self.inputs_digest}",
        ]
        if self.step_note:
            lines.append(f"  note        {self.step_note}")
        if self.screenshot:
            lines.append(f"  screenshot  {self.screenshot}")
        if self.snapshot:
            lines.append(f"  snapshot    {self.snapshot}")
        lines.append("  options:")
        lines += [f"    {option}" for option in self.options]
        return "\n".join(lines)


def _safely(fn, default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default
