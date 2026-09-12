"""What a replay returns to its caller.

The shape encodes the distinction the whole design turns on. A capability that
runs and learns "no such member" has **succeeded at finding out**; the caller
needs that answer, not an exception. A capability that cannot find a control it
was recorded against has failed, and the caller needs enough detail to fix it.
Collapsing those two into one channel is the mistake this contract exists to
make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["success", "business_outcome", "escalated", "failed"]

# Exit codes, so a shell caller can branch without parsing anything.
EXIT = {"success": 0, "failed": 1, "business_outcome": 3, "escalated": 4}


@dataclass
class StepReport:
    step_id: str
    action: str
    ok: bool
    strategy: str = ""
    attempts: list[str] = field(default_factory=list)
    detail: str = ""
    code: str = ""
    elapsed_ms: int = 0


@dataclass
class RecoveryReport:
    code: str
    step_id: str
    attempt: int
    resolved: bool
    then: str = ""


@dataclass
class Failure:
    code: str
    step_id: str
    action: str = ""
    expected: str = ""
    observed: str = ""
    locator_attempts: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code, "step_id": self.step_id, "action": self.action,
            "expected": self.expected, "observed": self.observed,
            "locator_attempts": self.locator_attempts, "evidence": self.evidence,
        }


@dataclass
class Outcome:
    code: str
    outcome: str
    message: str = ""
    step_id: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "outcome": self.outcome,
                "message": self.message, "step_id": self.step_id}


@dataclass
class ReplayResult:
    status: Status
    capability_id: str
    capability_version: str
    run_id: str = ""
    inputs_digest: str = ""
    outputs: dict[str, Any] | None = None
    outcome: Outcome | None = None
    error: Failure | None = None
    steps: list[StepReport] = field(default_factory=list)
    recoveries: list[RecoveryReport] = field(default_factory=list)
    control: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0
    elapsed_ms: int = 0

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "capability": {"id": self.capability_id, "version": self.capability_version},
            "run_id": self.run_id,
            "inputs_digest": self.inputs_digest,
            "outputs": self.outputs,
            "outcome": self.outcome.as_dict() if self.outcome else None,
            "error": self.error.as_dict() if self.error else None,
            "steps": [s.__dict__ for s in self.steps],
            "recoveries": [r.__dict__ for r in self.recoveries],
            "control": self.control,
            "llm_calls": self.llm_calls,
            "elapsed_ms": self.elapsed_ms,
        }


class ReplayPurityError(RuntimeError):
    """Something tried to consult a model on the replay path.

    Determinism is not a promise made in a comment. The engine is constructed
    with an object that raises on any use, so a future convenience - summarising
    an error, guessing a selector, deciding whether a page "looks right" - fails
    loudly the first time it is tried rather than quietly eroding the property
    the whole design rests on.
    """


class NoLLM:
    def __getattr__(self, name: str):
        raise ReplayPurityError(
            f"replay attempted to use a model ({name!r}). Replay makes no model "
            f"calls; if a decision cannot be made from the artifact, it is an "
            f"escalation, not an inference."
        )
