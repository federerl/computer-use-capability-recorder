"""Who is driving the session.

Automation and a person share one live browser, and the thing that must never
happen is both acting at once. The lease is the answer: a small file, a state, a
holder, and a monotonic sequence number.

The sequence number is a fencing token. Every action asserts the lease is still
held by the value it last saw, so an automation thread that was paused, handed
over, and somehow resumed against a stale view of the world fails loudly instead
of clicking into a session a person is using.

In one process this is an assertion rather than a distributed lock, and it is
worth saying so. The point of putting the token in now is that making it a real
lock later is a deployment change rather than a redesign.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ControlState(str, Enum):
    AUTOMATION = "automation"
    HANDOFF_REQUESTED = "handoff_requested"
    HUMAN = "human"
    RESUME_REQUESTED = "resume_requested"
    ABORTED = "aborted"


#: The only transitions that make sense. Anything else is a bug in the caller,
#: and the lease refuses it rather than recording an impossible history.
TRANSITIONS: dict[ControlState, set[ControlState]] = {
    ControlState.AUTOMATION: {ControlState.HANDOFF_REQUESTED},
    ControlState.HANDOFF_REQUESTED: {ControlState.HUMAN, ControlState.ABORTED},
    ControlState.HUMAN: {ControlState.RESUME_REQUESTED, ControlState.ABORTED},
    ControlState.RESUME_REQUESTED: {ControlState.AUTOMATION, ControlState.ABORTED},
    ControlState.ABORTED: set(),
}

#: How a person hands control back. The distinction matters: an approved
#: irreversible step performed twice is two stop payments.
RESUME_MODES = ("approve", "completed")


class ControlViolation(RuntimeError):
    """Something tried to act without holding the lease."""


class InvalidTransition(RuntimeError):
    pass


@dataclass
class Lease:
    run_id: str
    state: str = ControlState.AUTOMATION.value
    holder: str = "automation"
    seq: int = 0
    updated_at: str = ""
    operator: str = ""
    resume_mode: str = ""
    note: str = ""
    reason: str = ""
    step_id: str = ""

    @property
    def control(self) -> ControlState:
        return ControlState(self.state)


class ControlLease:
    """File-backed so an operator in another process can see and change it.

    A file is not a sophisticated channel, and that is the point: the handoff
    mechanism should be legible, and the interesting design is in the states and
    the revalidation, not in the transport.
    """

    def __init__(self, path: str | Path, run_id: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(Lease(run_id=run_id, updated_at=_now()))

    # ----------------------------------------------------------------- io

    def read(self, attempts: int = 5) -> Lease:
        """Read the lease, tolerating a writer mid-flight.

        The operator is a different process, so a reader can arrive between a
        truncate and a write. A brief retry is the right answer: a lease that
        momentarily looks empty is not a lease that says anything, and treating
        it as authoritative would be worse than waiting a few milliseconds.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return Lease(**json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, FileNotFoundError, TypeError) as exc:
                last = exc
                time.sleep(0.02 * (attempt + 1))
        raise RuntimeError(f"control file at {self.path} is unreadable: {last}")

    def _write(self, lease: Lease) -> Lease:
        """Replace the file rather than rewriting it in place, so a reader sees
        either the old lease or the new one and never half of either."""
        payload = json.dumps(asdict(lease), indent=2) + "\n"
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)
        return lease

    # --------------------------------------------------------- transitions

    def transition(self, to: ControlState, holder: str, **fields) -> Lease:
        current = self.read()
        if to not in TRANSITIONS[current.control]:
            raise InvalidTransition(
                f"{current.state} -> {to.value} is not a legal handover; "
                f"legal from here: {sorted(s.value for s in TRANSITIONS[current.control])}"
            )
        updated = Lease(
            run_id=current.run_id, state=to.value, holder=holder,
            seq=current.seq + 1, updated_at=_now(),
            operator=fields.get("operator", current.operator),
            resume_mode=fields.get("resume_mode", ""),
            note=fields.get("note", current.note),
            reason=fields.get("reason", current.reason),
            step_id=fields.get("step_id", current.step_id),
        )
        return self._write(updated)

    # ------------------------------------------------------------- guards

    def assert_automation_holds(self, expect_seq: int | None = None) -> None:
        """Called before every action that reaches the browser."""
        lease = self.read()
        if lease.control is not ControlState.AUTOMATION or lease.holder != "automation":
            raise ControlViolation(
                f"automation tried to act while control is {lease.state!r} "
                f"(held by {lease.holder!r})"
            )
        if expect_seq is not None and lease.seq != expect_seq:
            raise ControlViolation(
                f"control changed underneath this run (saw seq {expect_seq}, "
                f"lease is at {lease.seq}); the session is no longer the one "
                f"this step was planned against"
            )

    def wait_for(self, states: set[ControlState], timeout_s: float,
                 poll_s: float = 0.4, on_poll=None) -> Lease:
        """Block until the lease reaches one of `states`.

        `on_poll` exists so a caller sharing this thread can do work between
        polls; browser handles are not safe to touch from another thread, so a
        test that plays the operator has to run here rather than alongside.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            lease = self.read()
            if lease.control in states:
                return lease
            if on_poll is not None:
                on_poll(lease)
            time.sleep(poll_s)
        return self.read()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
