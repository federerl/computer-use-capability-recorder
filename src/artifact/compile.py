"""Turning a discovery run into a capability.

The recording already holds the hard part: targeting computed from what was on
screen, and values bound to the input they came from. Compilation is the step
that generalises what remains - the host a URL points at, a control chosen by a
value that happens to match an input - and, just as importantly, refuses to
generalise what it cannot verify.

A discovery run sees one path through an interface. It has not observed the
states it did not reach, and it cannot tell an incidental on-screen value from
one that is fixed for every caller. Where it cannot decide, the compiler records
the question in `review` and leaves the capability a draft. Approval is a human
act, and pretending otherwise would produce artifacts that look finished and are
not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.artifact.binding import TEMPLATABLE_FIELDS
from src.artifact.schema import (
    AppProfile, Capability, Checkpoint, Condition, DiscoveredBy, Extraction,
    OutputSpec, ParamSpec, Provenance, SecretRef, Step, StepPolicy, TextPresent,
    ValueMatches,
)
from src.discovery.agent import DiscoveryResult
from src.surface.base import Target


@dataclass
class CompileRequest:
    capability_id: str
    result: DiscoveryResult
    inputs: list[ParamSpec]
    secrets: list[SecretRef]
    app_profile: AppProfile
    base_url: str
    model: str
    trace_ref: str = ""
    version: str = "1.0.0"


def compile_capability(req: CompileRequest) -> Capability:
    result = req.result
    if not result.ok:
        raise ValueError(
            f"run did not complete ({result.status}: {result.detail}); there is "
            f"no successful flow to record"
        )

    finalize = result.finalize or {}
    values = {p.name: str(_example_value(p, result)) for p in req.inputs}
    review: list[str] = []

    steps = [_canonicalise(s, values, req.base_url, review) for s in result.steps]
    extractions, outputs = _outputs(finalize, result, review)
    checkpoint = _checkpoint(steps, finalize, outputs)
    conditions = _conditions(finalize)

    if not conditions:
        review.append(
            "No non-success outcomes were identified. Add the ones this flow can "
            "legitimately return before approving it."
        )
    for condition in conditions:
        detected = getattr(condition.detect, "value", "") or getattr(
            condition.detect, "name", "")
        review.append(
            f"Condition {condition.code} was anticipated, not observed: this run "
            f"never reached that state, so {detected!r} is a guess at wording "
            f"nobody has read. Confirm it against the real screen - a detector "
            f"that never fires leaves the state it was meant to catch falling "
            f"through as unknown."
        )
    review.append(
        "Recoverable conditions cannot be discovered from a successful run. Add "
        "handling for interstitials, timeouts and slow loads this application can "
        "produce."
    )

    used_by_secret: dict[str, list[str]] = {}
    for step in steps:
        if step.value and step.value.from_secret:
            used_by_secret.setdefault(step.value.from_secret, []).append(step.id)

    secrets = [
        SecretRef(name=s.name, ref=s.ref, used_in_steps=used_by_secret.get(s.name, []))
        for s in req.secrets
        if s.name in used_by_secret
    ]

    return Capability(
        id=req.capability_id,
        version=req.version,
        title=finalize.get("title") or req.capability_id,
        description=finalize.get("description", ""),
        approval_state="draft",
        app_profile=req.app_profile,
        inputs=req.inputs,
        outputs=outputs,
        secrets=secrets,
        steps=steps,
        extract=extractions,
        checkpoints=[checkpoint] if checkpoint else [],
        conditions=conditions,
        review=review,
        provenance=Provenance(
            discovered_by=DiscoveredBy(model=req.model, run_id=result.run_id,
                                       steps_taken=len(result.steps)),
            trace_ref=req.trace_ref,
            recorded_at=datetime.now(timezone.utc),
        ),
    )


# ------------------------------------------------------------ generalisation

def _example_value(param: ParamSpec, result: DiscoveryResult) -> Any:
    for step in result.steps:
        if step.value and step.value.from_input == param.name:
            break
    return param.example if param.example is not None else ""


def _canonicalise(step, values: dict[str, str], base_url: str,
                  review: list[str]) -> Step:
    url = step.url
    if url and base_url and url.startswith(base_url):
        url = "${BASE_URL}" + url[len(base_url):]

    target = _generalise_target(step.target, values, review, step.id) if step.target else None
    wait = _check_wait(step.wait, values, review, step.id)

    risk = getattr(step, "risk", "safe")
    policy = None
    note = step.note

    if risk != "safe":
        # The classification comes from the policy that permitted the action,
        # not from the run. A run watches a click succeed; it has no way to see
        # that the click committed something that cannot be undone.
        policy = StepPolicy(requires="confirmation")
        note = (note + " " if note else "") + (
            "Classified as irreversible by policy at record time.")
        review.append(
            f"Step {step.id}: classified {risk} by policy and gated on "
            f"confirmation. Confirm the classification is right - both a missed "
            f"one and a spurious one are expensive, in opposite ways."
        )

    return Step(
        id=step.id, action=step.action, target=target, url=url, value=step.value,
        key=step.key, wait=wait, risk=risk, policy=policy, note=note,
    )


def _check_wait(wait, values: dict[str, str], review: list[str], step_id: str):
    """Waits are derived from what an action changed, so they can pick up text
    belonging to the record the run happened to use.

    Two different problems live here. A wait carrying an input value can be
    found by comparison. A wait carrying something that was never an input - a
    member's name in a heading - cannot be, because there is nothing to compare
    it against. Both end up in review: the first named precisely, the second by
    listing the derived waits for a person to read.
    """
    text = wait.text or wait.name
    if not text:
        return wait

    for name, supplied in values.items():
        if supplied and supplied in text:
            review.append(
                f"Step {step_id}: waits for {text!r}, which contains the value "
                f"supplied for {name!r}. It will wait forever on any other "
                f"record. Replace it with text that is the same for every "
                f"invocation."
            )
            return wait

    review.append(
        f"Step {step_id}: waits for {text!r}, learned from what this run "
        f"changed. Confirm that text appears for every invocation and is not "
        f"particular to this record."
    )
    return wait


def _generalise_target(target: Target, values: dict[str, str],
                       review: list[str], step_id: str) -> Target:
    """Replace a targeting string that *is* an input with a placeholder.

    Only an exact match is rewritten. A string that merely contains an input
    value - a results row reading "100482 BARNES, R. Checking ****4821" - would
    still carry the rest of that member's details after substitution, so
    rewriting it would produce a target that looks parameterised and works for
    exactly one member. Those are reported instead.
    """
    data = target.model_dump()

    def walk(node: Any, path: str = "") -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if isinstance(value, str) and key in TEMPLATABLE_FIELDS and value:
                    out[key] = _rewrite(value, values, review, step_id, key)
                else:
                    out[key] = walk(value, key)
            return out
        if isinstance(node, list):
            return [walk(v, path) for v in node]
        return node

    return Target.model_validate(walk(data))


def _rewrite(value: str, values: dict[str, str], review: list[str],
             step_id: str, field: str) -> str:
    for name, supplied in values.items():
        if not supplied:
            continue
        if value == supplied:
            return "${" + name + "}"
        if supplied in value:
            review.append(
                f"Step {step_id}: targets {field}={value!r}, which contains the "
                f"value supplied for {name!r} alongside other details from this "
                f"run. It will not generalise as recorded. Re-anchor it on "
                f"something stable, or confirm this capability is single-record."
            )
    return value


# ------------------------------------------------------------------- outputs

def _outputs(finalize: dict, result: DiscoveryResult,
             review: list[str]) -> tuple[list[Extraction], list[OutputSpec]]:
    extractions: list[Extraction] = []
    outputs: list[OutputSpec] = []

    for spec in finalize.get("outputs", []):
        name = spec["name"]
        target = result.output_targets.get(name)
        if target is None:
            review.append(f"Output {name!r} has no usable target and was dropped.")
            continue

        declared_type = spec.get("type", "string")
        extractions.append(Extraction(
            name=name, target=target,
            transform="number" if declared_type in ("number", "integer") else "trim",
        ))
        outputs.append(OutputSpec(
            name=name, type=declared_type, pattern=spec.get("pattern") or None,
            description=spec.get("description", ""),
        ))

    return extractions, outputs


def _checkpoint(steps: list[Step], finalize: dict,
                outputs: list[OutputSpec]) -> Checkpoint | None:
    if not steps:
        return None

    success_text = (finalize.get("success_text") or "").strip()
    frame = finalize.get("success_frame") or "main"
    assertions: list[Any] = []

    if success_text:
        assertions.append(TextPresent(value=success_text, scope={"frame": frame}))

    # A value that arrived in the expected shape is stronger evidence than text
    # alone: a page can carry the right words and still not be the right page.
    assertions += [
        ValueMatches(extract=o.name, pattern=o.pattern)
        for o in outputs if o.pattern
    ]

    if not assertions:
        return None
    return Checkpoint(after_step=steps[-1].id, assertions=assertions)


def _conditions(finalize: dict) -> list[Condition]:
    """Non-success results the run identified.

    Everything here is a business outcome. A successful run cannot confirm that
    a recovery works, so the compiler does not invent one; those are authored in
    review, which is what keeps the capability honest about what was actually
    observed.
    """
    conditions = []
    for spec in finalize.get("anticipated_conditions", []):
        conditions.append(Condition.model_validate({
            "code": spec["code"],
            "class": "business_outcome",
            "applies_to": "any",
            "detect": {"kind": "text_present", "value": spec["text"],
                       "scope": {"frame": spec.get("frame", "main")}},
            "outcome": spec["outcome"],
            "message": spec.get("message", ""),
            "terminal": True,
            "verified": False,
        }))
    return conditions
