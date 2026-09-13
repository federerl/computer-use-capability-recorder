"""Deterministic replay: the path an agent actually invokes.

No model is consulted, and the engine is built so none can be. Every decision
comes from the artifact: which control to act on, what counts as done, which
runtime states are answers, which are recoverable, and which are failures.

The loop is ordinary. What matters is what it refuses to do. It does not pick
between two matching controls, does not continue past a checkpoint that did not
hold, does not treat an unrecognised state as fine, and does not repeat an
irreversible action to get back on track.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from src.artifact.binding import bind_target, read_secret
from src.artifact.schema import (
    Capability, Condition, Extraction, ParamSpec, Step, ValueSource,
)
from src.replay import conditions as cond
from src.replay.result import (
    Failure, NoLLM, Outcome, RecoveryReport, ReplayResult, StepReport,
)
from src.surface.base import (
    ActionBlocked, ClickAction, ConfirmationRequired, FillAction, NavigateAction,
    PressAction, SelectAction, SurfaceError, TargetNotFound,
)

MAX_CONDITION_LOOPS = 12


class InputError(ValueError):
    """The caller's arguments do not satisfy the capability's contract."""


class Escalation(Exception):
    """Replay cannot safely continue and a person must decide.

    Raised rather than returned so it cannot be mistaken for a result. Handled
    by the caller that owns the session.
    """

    def __init__(self, code: str, step_id: str, detail: str):
        self.code, self.step_id, self.detail = code, step_id, detail
        super().__init__(f"{code} at {step_id}: {detail}")


def validate_inputs(capability: Capability, supplied: dict[str, Any]) -> dict[str, Any]:
    """Check the caller's arguments before touching the application.

    Rejecting here rather than partway through matters: a run that fails on step
    nine has already done eight things to a customer record.
    """
    declared = {p.name: p for p in capability.inputs}
    unknown = set(supplied) - set(declared)
    if unknown:
        raise InputError(f"unknown inputs {sorted(unknown)}; "
                         f"declared: {sorted(declared)}")

    resolved: dict[str, Any] = {}
    for name, spec in declared.items():
        if name not in supplied or supplied[name] is None:
            if spec.required:
                raise InputError(f"missing required input {name!r}")
            continue
        resolved[name] = _coerce(spec, supplied[name])
    return resolved


def _coerce(spec: ParamSpec, value: Any) -> Any:
    if spec.type in ("integer", "number"):
        try:
            value = int(value) if spec.type == "integer" else float(value)
        except (TypeError, ValueError):
            raise InputError(f"{spec.name!r} must be a {spec.type}, got {value!r}")
    elif spec.type == "boolean":
        if not isinstance(value, bool):
            raise InputError(f"{spec.name!r} must be a boolean, got {value!r}")
    else:
        value = str(value)
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            raise InputError(
                f"{spec.name!r} must match {spec.pattern} , got {value!r}")
        if spec.enum and value not in spec.enum:
            raise InputError(f"{spec.name!r} must be one of {spec.enum}, got {value!r}")
    return value


def digest(inputs: dict[str, Any]) -> str:
    """Correlate runs without keeping what they were about."""
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


class ReplayEngine:
    def __init__(self, surface, capability: Capability, log, *,
                 runtime: dict[str, str] | None = None, llm: Any = None,
                 allow_escalation: bool = False, auto_confirm: bool = False) -> None:
        self.surface = surface
        self.capability = capability
        self.log = log
        self.runtime = runtime or {}
        self.llm = llm if llm is not None else NoLLM()
        self.allow_escalation = allow_escalation
        self.auto_confirm = auto_confirm

        self.llm_calls = 0
        self._inputs: dict[str, Any] = {}
        self._secrets: dict[str, str] = {}
        self._extracted: dict[str, Any] = {}
        self._steps: list[StepReport] = []
        self._recoveries: list[RecoveryReport] = []
        self._attempts: dict[str, int] = {}
        self._irreversible_done = False

    # ------------------------------------------------------------------ run

    def run(self, inputs: dict[str, Any]) -> ReplayResult:
        started = time.monotonic()
        self._inputs = validate_inputs(self.capability, inputs)

        for secret in self.capability.secrets:
            value = read_secret(secret.ref)
            self._secrets[secret.name] = value
            self.log.redactor.register_secret(secret.name, value)

        self.log.event("replay_started", capability=self.capability.id,
                       version=self.capability.version,
                       inputs_digest=digest(self._inputs),
                       approval_state=self.capability.approval_state)

        try:
            status, payload = self._execute()
        except Escalation as esc:
            self.log.event("escalated", code=esc.code, step=esc.step_id,
                           detail=esc.detail)
            return self._result("escalated", started, error=Failure(
                code=esc.code, step_id=esc.step_id, observed=esc.detail))

        if status == "success":
            return self._result("success", started, outputs=payload)
        if status == "business_outcome":
            return self._result("business_outcome", started, outcome=payload)
        return self._result("failed", started, error=payload)

    def _execute(self):
        index = 0
        loops = 0
        steps = self.capability.steps

        while index < len(steps):
            loops += 1
            if loops > len(steps) + MAX_CONDITION_LOOPS:
                return "failed", Failure(
                    code="RECOVERY_LOOP", step_id=steps[index].id,
                    expected="progress", observed="conditions kept re-firing")

            step = steps[index]

            # States that are not tied to a step - an interstitial, a timeout -
            # are checked before acting, because acting through one is how a
            # click lands somewhere unintended.
            outcome = self._check_conditions(step, before=True)
            if outcome is not None:
                kind, value = outcome
                if kind == "terminal":
                    return value
                if kind == "restart":
                    index = 0
                    continue
                if kind == "retry":
                    continue

            report = self._perform(step)
            self._steps.append(report)
            if not report.ok:
                return "failed", self._failure_from(step, report)

            outcome = self._check_conditions(step, before=False)
            if outcome is not None:
                kind, value = outcome
                if kind == "terminal":
                    return value
                if kind == "restart":
                    index = 0
                    continue
                if kind == "retry":
                    continue

            checkpoint_failure = self._verify_checkpoints(step)
            if checkpoint_failure is not None:
                return "failed", checkpoint_failure

            index += 1

        self._extract_all()
        missing = [o.name for o in self.capability.outputs
                   if o.required and o.name not in self._extracted]
        if missing:
            return "failed", Failure(
                code="OUTPUT_MISSING", step_id=steps[-1].id,
                expected=f"values for {missing}",
                observed="the final screen did not yield them")
        return "success", dict(self._extracted)

    # ---------------------------------------------------------------- steps

    def _perform(self, step: Step) -> StepReport:
        started = time.monotonic()

        # A step the capability marks as needing a decision is not something
        # replay is entitled to take on its own. Whether that means stopping or
        # bringing in a person is the caller's policy, not the engine's.
        if step.policy and step.policy.requires == "confirmation" and not self.auto_confirm:
            if self.allow_escalation:
                raise Escalation(
                    "CONFIRMATION_REQUIRED", step.id,
                    f"{step.action} on {step.id} is marked {step.risk} and the "
                    f"capability requires a person to confirm it"
                    + (f": {step.note}" if step.note else ""))
            return StepReport(
                step_id=step.id, action=step.action, ok=False,
                code="CONFIRMATION_REQUIRED",
                detail=f"step is marked {step.risk} and requires confirmation, "
                       f"which this invocation cannot obtain")

        try:
            action = self._build_action(step)
        except Exception as exc:
            return StepReport(step_id=step.id, action=step.action, ok=False,
                              code="STEP_UNBUILDABLE",
                              detail=f"{type(exc).__name__}: {exc}")

        try:
            result = self.surface.act(action)
        except TargetNotFound as exc:
            return StepReport(
                step_id=step.id, action=step.action, ok=False,
                code="TARGET_NOT_FOUND",
                attempts=[a.describe() for a in exc.attempts],
                detail="no unique match for the recorded target",
                elapsed_ms=int((time.monotonic() - started) * 1000))
        except ConfirmationRequired as exc:
            if self.allow_escalation:
                raise Escalation("CONFIRMATION_REQUIRED", step.id, str(exc)) from exc
            return StepReport(step_id=step.id, action=step.action, ok=False,
                              code="CONFIRMATION_REQUIRED", detail=str(exc))
        except ActionBlocked as exc:
            return StepReport(step_id=step.id, action=step.action, ok=False,
                              code="POLICY_VIOLATION", detail=str(exc))
        except SurfaceError as exc:
            return StepReport(step_id=step.id, action=step.action, ok=False,
                              code="STEP_FAILED",
                              detail=f"{type(exc).__name__}: {exc}")

        satisfied = cond.wait_for(self.surface, step.wait)
        if step.risk == "irreversible":
            self._irreversible_done = True

        report = StepReport(
            step_id=step.id, action=step.action, ok=True,
            strategy=result.strategy_used.by if result.strategy_used else "",
            attempts=[a.describe() for a in result.attempts],
            detail="" if satisfied else
            f"wait for {cond.describe_wait(step.wait)} timed out",
            elapsed_ms=int((time.monotonic() - started) * 1000))

        self.log.event("step", step_id=step.id, action=step.action,
                       strategy=report.strategy, waited=satisfied,
                       url=self.surface.url())
        return report

    def _build_action(self, step: Step):
        if step.action == "navigate":
            from src.artifact.binding import resolve
            return NavigateAction(url=resolve(step.url or "", self._inputs, self.runtime))

        target = bind_target(step.target, self._inputs, self.runtime)

        if step.action == "click":
            return ClickAction(target=target)
        if step.action == "press":
            return PressAction(target=target, key=step.key or "Enter")

        value = self._value_of(step.value)
        if step.action == "fill":
            return FillAction(target=target, value=value)
        return SelectAction(target=target, value=value)

    def _value_of(self, source: ValueSource | None) -> str:
        if source is None:
            return ""
        if source.from_input:
            return str(self._inputs[source.from_input])
        if source.from_secret:
            return self._secrets[source.from_secret]
        return source.literal or ""

    # ----------------------------------------------------------- conditions

    def _check_conditions(self, step: Step, *, before: bool):
        applicable = [
            c for c in self.capability.conditions_for(step.id)
            if not before or c.applies_to == "any"
        ]
        match = cond.first_match(applicable, self.surface, self._extracted)
        if match is None:
            return None

        self.log.event("condition_matched", code=match.code, klass=match.klass,
                       step=step.id, detector=cond.describe(match.detect))

        if match.klass == "business_outcome":
            return "terminal", ("business_outcome", Outcome(
                code=match.code, outcome=match.outcome or "",
                message=match.message, step_id=step.id))

        if match.klass == "hard":
            return "terminal", ("failed", Failure(
                code=match.code, step_id=step.id, action=step.action,
                expected="a state the capability knows how to continue from",
                observed=cond.observed(match.detect, self.surface, self._extracted)))

        return self._recover(match, step, before=before)

    def _recover(self, condition: Condition, step: Step, *, before: bool):
        key = f"{condition.code}:{step.id}"
        self._attempts[key] = self._attempts.get(key, 0) + 1
        attempt = self._attempts[key]

        if attempt > condition.max_attempts:
            self._recoveries.append(RecoveryReport(
                code=condition.code, step_id=step.id, attempt=attempt, resolved=False))
            return "terminal", ("failed", Failure(
                code=f"{condition.code}_UNRECOVERED", step_id=step.id,
                expected=f"recovery to clear it within {condition.max_attempts} "
                         f"attempt(s)",
                observed=cond.observed(condition.detect, self.surface, self._extracted)))

        recovery = condition.recover
        if recovery and recovery.then == "restart_flow" and self._irreversible_done:
            # Re-running the flow would repeat something that cannot be undone.
            raise Escalation(
                "RESTART_WOULD_REPEAT_IRREVERSIBLE", step.id,
                f"{condition.code} asks to restart, but an irreversible step has "
                f"already run in this invocation")

        for recovery_step in (recovery.steps if recovery else []):
            report = self._perform(recovery_step)
            self._steps.append(report)
            if not report.ok:
                self._recoveries.append(RecoveryReport(
                    code=condition.code, step_id=step.id, attempt=attempt,
                    resolved=False))
                return "terminal", ("failed", self._failure_from(recovery_step, report))

        then = recovery.then if recovery else "retry_current_step"

        # A condition found *after* the step ran means the step already happened.
        # Retrying would repeat it, and repeating a click that submitted a form
        # is how one instruction becomes two. Clearing the obstruction and
        # carrying on is the only safe reading.
        if not before and then == "retry_current_step":
            then = "continue"

        self._recoveries.append(RecoveryReport(
            code=condition.code, step_id=step.id, attempt=attempt, resolved=True,
            then=then))
        self.log.event("recovered", code=condition.code, step=step.id,
                       attempt=attempt, then=then)

        if then == "restart_flow":
            self._extracted.clear()
            return "restart", None
        if then == "continue":
            return None
        return "retry", None

    # ---------------------------------------------------------- checkpoints

    def _verify_checkpoints(self, step: Step) -> Failure | None:
        for checkpoint in self.capability.checkpoints:
            if checkpoint.after_step != step.id:
                continue

            # Extract first: a checkpoint may assert on the shape of a value,
            # which is stronger evidence than text that happens to be present.
            self._extract_all()

            for assertion in checkpoint.assertions:
                if cond.evaluate(assertion, self.surface, self._extracted):
                    continue
                self.log.event("checkpoint_failed", step=step.id,
                               expected=cond.describe(assertion))
                return Failure(
                    code="CHECKPOINT_FAILED", step_id=step.id, action=step.action,
                    expected=cond.describe(assertion),
                    observed=cond.observed(assertion, self.surface, self._extracted),
                    evidence=self._capture(step.id))
            self.log.event("checkpoint_passed", step=step.id,
                           assertions=len(checkpoint.assertions))
        return None

    # ----------------------------------------------------------- extraction

    def _extract_all(self) -> None:
        for extraction in self.capability.extract:
            try:
                self._extracted[extraction.name] = self._extract(extraction)
            except (TargetNotFound, SurfaceError, ValueError):
                continue

    def _extract(self, extraction: Extraction) -> Any:
        target = bind_target(extraction.target, self._inputs, self.runtime)
        locator = self.surface.resolve(target).locator

        if extraction.take == "value":
            raw = locator.input_value()
        elif extraction.take == "attribute":
            raw = locator.get_attribute(extraction.attribute or "") or ""
        else:
            raw = locator.inner_text()

        return _transform(raw, extraction)

    # -------------------------------------------------------------- results

    def _failure_from(self, step: Step, report: StepReport) -> Failure:
        return Failure(
            code=report.code or "STEP_FAILED", step_id=step.id, action=step.action,
            expected=f"{step.action} on the recorded target",
            observed=report.detail, locator_attempts=report.attempts,
            evidence=self._capture(step.id))

    def _capture(self, step_id: str) -> dict[str, str]:
        """Richer evidence, captured at the moment of failure rather than
        reconstructed from a log afterwards."""
        evidence: dict[str, str] = {}
        try:
            path = self.log.dir / "failure" / f"{step_id}.png"
            evidence["screenshot"] = self.log.relative(self.surface.screenshot(path))
        except Exception:
            pass
        try:
            from src.surface.snapshot import render
            path = self.log.write(f"failure/{step_id}.txt",
                                  render(self.surface.snapshot()))
            evidence["snapshot"] = self.log.relative(path)
        except Exception:
            pass
        evidence["url"] = self.surface.url()
        return evidence

    def _result(self, status: str, started: float, *, outputs=None, outcome=None,
                error=None) -> ReplayResult:
        result = ReplayResult(
            status=status, capability_id=self.capability.id,
            capability_version=self.capability.version, run_id=self.log.run_id,
            inputs_digest=digest(self._inputs), outputs=outputs, outcome=outcome,
            error=error, steps=self._steps, recoveries=self._recoveries,
            llm_calls=self.llm_calls,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        self.log.event("replay_finished", status=status,
                       outputs=outputs, outcome=outcome.as_dict() if outcome else None,
                       error=error.as_dict() if error else None,
                       llm_calls=self.llm_calls)
        self.log.write("result.json", result.as_dict())
        return result


def _transform(raw: str, extraction: Extraction) -> Any:
    text = (raw or "").strip()

    if extraction.transform == "first_match":
        match = re.search(extraction.pattern or "", text)
        if not match:
            raise ValueError(f"{extraction.name}: no match for {extraction.pattern!r}")
        return match.group(0)

    if extraction.transform in ("number", "currency"):
        cleaned = re.sub(r"[^0-9.\-]", "", text)
        if not cleaned:
            raise ValueError(f"{extraction.name}: {text!r} holds no number")
        return float(cleaned)

    return text
