"""Bringing a person into a run, and taking the session back afterwards.

The sequence is: stop acting, hand over enough context to act on, give up the
lease, let the person work the same live session, then revalidate before doing
anything else.

Two decisions here are the ones worth defending.

**Resuming distinguishes "I approve, you do it" from "I already did it."**
Without that distinction, handing back after approving an irreversible step would
place a second stop payment. It cannot be inferred - only the person knows - so
it is part of how they hand back.

**State is revalidated before automation moves again.** The person had a live
browser and may have gone somewhere else entirely. Carrying on because the lease
says it is our turn would be acting on an assumption that was true several
minutes and several clicks ago.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.escalation.control import ControlLease, ControlState
from src.escalation.intervention import Intervention
from src.escalation.recorder import HumanActionRecorder

Verdict = Literal["approved", "completed", "aborted", "timed_out", "state_mismatch"]


@dataclass
class Handover:
    verdict: Verdict
    intervention_id: str
    operator: str = ""
    note: str = ""
    human_actions: int = 0
    detail: str = ""


class HumanSupervisor:
    """Holds the seam between a stuck run and a person."""

    def __init__(self, lease: ControlLease, log, *, timeout_s: float = 900,
                 poll_s: float = 0.4, on_wait=None) -> None:
        self.lease = lease
        self.log = log
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.on_wait = on_wait
        self.handovers: list[Handover] = []
        self._recorder: HumanActionRecorder | None = None

    def handle(self, *, code: str, step_id: str, detail: str, surface,
               capability, inputs_digest: str) -> Handover:
        intervention = Intervention.build(
            run_id=self.lease.read().run_id or self.log.run_id,
            capability=capability, step_id=step_id, code=code, detail=detail,
            inputs_digest=inputs_digest, surface=surface, log=self.log,
        )
        intervention.write(self.log.dir)

        self.log.event("intervention_raised", intervention=intervention.id,
                       code=code, step=step_id, detail=detail,
                       url=intervention.url)

        # One recorder per session, reinstalled each time: a run can hand over
        # more than once, and the page binding may only be registered once.
        if self._recorder is None or self._recorder.page is not surface.page:
            self._recorder = HumanActionRecorder(page=surface.page,
                                                 redactor=self.log.redactor)
        recorder = self._recorder
        already = len(recorder.actions)
        recorder.start()

        # Automation stops acting here. Nothing below touches the page until the
        # lease comes back.
        self.lease.transition(ControlState.HANDOFF_REQUESTED, holder="automation",
                              reason=code, step_id=step_id)
        self.lease.transition(ControlState.HUMAN, holder="human",
                              reason=code, step_id=step_id)

        lease = self.lease.wait_for(
            {ControlState.RESUME_REQUESTED, ControlState.ABORTED},
            timeout_s=self.timeout_s, poll_s=self.poll_s, on_poll=self.on_wait,
        )

        actions = recorder.actions[already:]
        recorder.write(self.log.dir / "human_actions.jsonl")
        for line in recorder.summary()[already:]:
            self.log.event("human_action", intervention=intervention.id, did=line)

        handover = self._settle(lease, intervention, actions, surface, capability,
                                step_id)
        self.handovers.append(handover)
        self.log.event("handover_complete", intervention=intervention.id,
                       verdict=handover.verdict, operator=handover.operator,
                       note=handover.note, human_actions=handover.human_actions,
                       detail=handover.detail)
        return handover

    # ------------------------------------------------------------ settling

    def _settle(self, lease, intervention, actions, surface, capability,
                step_id: str) -> Handover:
        base = dict(intervention_id=intervention.id, operator=lease.operator,
                    note=lease.note, human_actions=len(actions))

        if lease.control is ControlState.ABORTED:
            return Handover(verdict="aborted", **base)

        if lease.control is not ControlState.RESUME_REQUESTED:
            return Handover(verdict="timed_out",
                            detail=f"no response within {self.timeout_s:.0f}s", **base)

        self.lease.transition(ControlState.AUTOMATION, holder="automation")

        mismatch = self._revalidate(surface, capability, step_id,
                                    completed=lease.resume_mode == "completed")
        if mismatch:
            return Handover(verdict="state_mismatch", detail=mismatch, **base)

        verdict = "completed" if lease.resume_mode == "completed" else "approved"
        return Handover(verdict=verdict, **base)

    def _revalidate(self, surface, capability, step_id: str,
                    completed: bool) -> str | None:
        """Confirm the session is still somewhere this run can continue from.

        For a step about to be performed, the check is that its target resolves:
        if the control is not there, the person is not where they were. For a
        step the person performed themselves, the check is the step's own
        checkpoint, because that is the definition of it having worked.
        """
        from src.artifact.binding import bind_target
        from src.replay import conditions as cond
        from src.surface.base import SurfaceError, TargetNotFound

        step = capability.step(step_id)

        if completed:
            # Only the assertions about what is on screen. Anything asserting
            # the shape of an extracted value needs the extraction the engine
            # performs, and the engine re-verifies the whole checkpoint the
            # moment control returns - so this is the cheap "did anything
            # happen at all" check, not the authoritative one.
            for checkpoint in capability.checkpoints:
                if checkpoint.after_step != step_id:
                    continue
                for assertion in checkpoint.assertions:
                    if assertion.kind == "matches":
                        continue
                    try:
                        satisfied = cond.evaluate(assertion, surface, {})
                    except Exception:
                        satisfied = False
                    if not satisfied:
                        return (f"operator reported the step done, but "
                                f"{cond.describe(assertion)} does not hold")
            return None

        if step.target is None:
            return None
        try:
            surface.resolve(bind_target(step.target, {}, {}))
        except (TargetNotFound, SurfaceError) as exc:
            return (f"the control step {step_id} acts on is no longer resolvable "
                    f"after the handover: {exc}")
        except Exception:
            # Unbound placeholders mean the target depends on inputs the
            # supervisor does not hold; the engine rechecks when it acts.
            return None
        return None
