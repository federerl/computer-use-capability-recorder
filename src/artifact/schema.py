"""The capability artifact: a typed, versioned, reviewable description of a flow.

This is the contract between a discovery run and everything that comes after it.
An agent calls a capability; a person reviews one; the replay engine executes
one. It is deliberately not a transcript - the model's reasoning is referenced
by `provenance.trace_ref`, never embedded, so the capability can be read,
diffed and approved on its own terms.

Three decisions shape the rest of the file.

**Runtime conditions are declared, not inferred.** Each one carries its own
class - a business outcome the caller needs, something recoverable, or a hard
failure - decided when the flow is recorded and reviewed by a person. Anything
that matches no declared condition is by definition unknown, and unknown is
never treated as success. Conflating "no such member" with a crash is the
failure mode this shape exists to prevent.

**Detectors come from a closed set.** Six of them, all evaluable against a
snapshot with no model in the loop. A closed set is auditable; an open one would
smuggle judgement into replay.

**Values are bound by reference.** A step names an input or a secret; it never
carries a literal that came from the task. Parameterisation is therefore a
property of the recording rather than something reconstructed afterwards, and a
credential cannot reach the artifact by accident.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.surface.base import FrameScope, Target

SCHEMA_VERSION = "1.0.0"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_CAPABILITY_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

# A literal that looks like regulated data never belongs in a recorded flow.
# The schema refuses it rather than trusting every future caller to notice.
_LOOKS_SENSITIVE = (
    (re.compile(r"^\d{3}-\d{2}-\d{4}$"), "social security number"),
    (re.compile(r"^\d{13,19}$"), "card number"),
)

Sensitivity = Literal["none", "pii_id", "account", "secret"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------- contract

class ParamSpec(Strict):
    """One input the caller supplies per invocation."""
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    description: str = ""
    sensitivity: Sensitivity = "none"
    example: str | None = None


class OutputSpec(Strict):
    """One value the caller gets back."""
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = True
    pattern: str | None = None
    description: str = ""
    sensitivity: Sensitivity = "none"


class SecretRef(Strict):
    """A credential the flow needs, named but never carried.

    Only `env:` is supported. Adding a scheme is a deliberate act, which is the
    point: there is no way to write a secret value into an artifact.
    """
    name: str
    ref: str
    used_in_steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_scheme(self) -> SecretRef:
        if not self.ref.startswith("env:") or len(self.ref) <= 4:
            raise ValueError(
                f"secret {self.name!r}: ref must be 'env:NAME', got {self.ref!r}"
            )
        return self


class ValueSource(Strict):
    """Where a step's value comes from. Exactly one of these is set."""
    from_input: str | None = None
    from_secret: str | None = None
    literal: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ValueSource:
        set_fields = [f for f in ("from_input", "from_secret", "literal")
                      if getattr(self, f) is not None]
        if len(set_fields) != 1:
            raise ValueError(
                f"a value must come from exactly one source, got {set_fields or 'none'}"
            )
        if self.literal is not None:
            for pattern, what in _LOOKS_SENSITIVE:
                if pattern.match(self.literal.strip()):
                    raise ValueError(
                        f"literal looks like a {what}; bind it to an input or a "
                        f"secret instead of recording it"
                    )
        return self

    def describe(self) -> str:
        if self.from_input:
            return f"input:{self.from_input}"
        if self.from_secret:
            return f"secret:{self.from_secret}"
        return f"literal:{self.literal!r}"


# ------------------------------------------------------------------ detectors

class TextPresent(Strict):
    kind: Literal["text_present"] = "text_present"
    value: str
    scope: FrameScope = Field(default_factory=FrameScope)


class TextAbsent(Strict):
    kind: Literal["text_absent"] = "text_absent"
    value: str
    scope: FrameScope = Field(default_factory=FrameScope)


class RoleVisible(Strict):
    kind: Literal["role_visible"] = "role_visible"
    role: str
    name: str | None = None
    scope: FrameScope = Field(default_factory=FrameScope)


class UrlMatches(Strict):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class ValueMatches(Strict):
    """Assert an extracted value has the shape it is supposed to have. This is
    what stops a checkpoint passing on a page that merely contains the right
    words."""
    kind: Literal["matches"] = "matches"
    extract: str
    pattern: str


class CountAtLeast(Strict):
    kind: Literal["count_at_least"] = "count_at_least"
    role: str
    name: str | None = None
    minimum: int = 1
    scope: FrameScope = Field(default_factory=FrameScope)


Detector = Annotated[
    Union[TextPresent, TextAbsent, RoleVisible, UrlMatches, ValueMatches, CountAtLeast],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------- steps

class WaitSpec(Strict):
    """What "done" means for a step. Declared per step, because a generic
    quiescence heuristic cannot know that submitting a form should end on a
    confirmation rather than merely stop changing."""
    until: Literal["settled", "text", "url", "role"] = "settled"
    text: str | None = None
    pattern: str | None = None
    role: str | None = None
    name: str | None = None
    scope: FrameScope = Field(default_factory=FrameScope)
    timeout_ms: int = 8_000


class StepPolicy(Strict):
    requires: Literal["confirmation"] | None = None


class Step(Strict):
    id: str
    action: Literal["click", "fill", "select", "press", "navigate"]
    target: Target | None = None
    url: str | None = None
    value: ValueSource | None = None
    key: str | None = None
    wait: WaitSpec = Field(default_factory=WaitSpec)
    risk: Literal["safe", "irreversible"] = "safe"
    policy: StepPolicy | None = None
    note: str = ""

    @model_validator(mode="after")
    def _shape_matches_action(self) -> Step:
        if self.action == "navigate":
            if not self.url:
                raise ValueError(f"step {self.id}: navigate needs a url")
            if self.target is not None:
                raise ValueError(f"step {self.id}: navigate does not take a target")
            return self

        if self.target is None:
            raise ValueError(f"step {self.id}: {self.action} needs a target")
        if self.action in ("fill", "select") and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} needs a value")
        if self.action == "press" and not self.key:
            raise ValueError(f"step {self.id}: press needs a key")
        return self


class Checkpoint(Strict):
    """Proof the step did what it was supposed to. Without one, replay is
    asserting that a click was dispatched, not that anything happened."""
    after_step: str
    assertions: list[Detector] = Field(min_length=1)


class Extraction(Strict):
    name: str
    target: Target
    take: Literal["text", "value", "attribute"] = "text"
    attribute: str | None = None
    transform: Literal["trim", "number", "currency", "first_match"] = "trim"
    pattern: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Extraction:
        if self.take == "attribute" and not self.attribute:
            raise ValueError(f"extraction {self.name!r}: take=attribute needs a name")
        if self.transform == "first_match" and not self.pattern:
            raise ValueError(f"extraction {self.name!r}: first_match needs a pattern")
        return self


class Recovery(Strict):
    """What to do about a recoverable condition.

    Steps are inline rather than a reference to another capability: dismissing an
    interstitial and signing back in are both short, and indirection would buy
    nothing here.

    `restart_flow` exists because some recoveries land somewhere else entirely -
    signing back in after a timeout returns to the entry screen, not to the form
    the run was halfway through, so retrying the current step would act on the
    wrong page. Restarting is refused once a step marked irreversible has
    executed; that run escalates instead of quietly doing the same irreversible
    thing twice.
    """
    steps: list[Step] = Field(default_factory=list)
    then: Literal["retry_current_step", "continue", "restart_flow"] = "retry_current_step"


class Condition(Strict):
    """A runtime state the flow knows how to name.

    `klass` is the whole point of the artifact carrying conditions at all. It is
    authored when the flow is recorded and reviewed by a person, so a caller
    receiving "no such member" gets an outcome rather than an exception, and an
    unexpected dialog does not get silently clicked through.
    """
    code: str
    klass: Literal["business_outcome", "recoverable", "hard"] = Field(alias="class")
    detect: Detector
    applies_to: Literal["any"] | list[str] = "any"
    message: str = ""
    outcome: str | None = None
    recover: Recovery | None = None
    max_attempts: int = 1
    terminal: bool = False

    @model_validator(mode="after")
    def _class_shape(self) -> Condition:
        if self.klass == "recoverable" and self.recover is None:
            raise ValueError(
                f"condition {self.code}: recoverable without a recovery is just a "
                f"hard failure with a friendlier name"
            )
        if self.klass == "business_outcome":
            if self.recover is not None:
                raise ValueError(
                    f"condition {self.code}: a business outcome is an answer, not "
                    f"something to recover from"
                )
            if not self.terminal:
                raise ValueError(
                    f"condition {self.code}: a business outcome ends the run"
                )
            if not self.outcome:
                raise ValueError(
                    f"condition {self.code}: a business outcome needs an outcome "
                    f"name for the caller to branch on"
                )
        return self


# ------------------------------------------------------------------- envelope

class SurfaceSpec(Strict):
    kind: Literal["web", "desktop"] = "web"
    engine: str = "chromium"


class AppProfile(Strict):
    """What the capability was recorded against.

    `product` is the vendor application; `tenant` is one institution's instance
    of it. Keeping them apart is what makes a capability shareable: institutions
    running the same product should reuse one recording with per-tenant
    overrides, not each own a private copy of the same flow.
    """
    product: str
    product_version: str = ""
    tenant: str
    entry: str


class DiscoveredBy(Strict):
    model: str
    run_id: str
    steps_taken: int = 0


class Provenance(Strict):
    discovered_by: DiscoveredBy | None = None
    trace_ref: str = ""
    recorded_at: datetime | None = None
    human_edits: list[str] = Field(default_factory=list)


class Capability(Strict):
    schema_version: str = SCHEMA_VERSION
    id: str
    version: str = "1.0.0"
    title: str
    description: str = ""
    approval_state: Literal["draft", "approved"] = "draft"

    surface: SurfaceSpec = Field(default_factory=SurfaceSpec)
    app_profile: AppProfile

    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)

    steps: list[Step] = Field(min_length=1)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    extract: list[Extraction] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)

    provenance: Provenance = Field(default_factory=Provenance)

    # ------------------------------------------------------------ consistency

    @model_validator(mode="after")
    def _internally_consistent(self) -> Capability:
        if not _CAPABILITY_ID.match(self.id):
            raise ValueError(f"capability id {self.id!r} is not a dotted lowercase name")
        for field, value in (("version", self.version),
                             ("schema_version", self.schema_version)):
            if not _SEMVER.match(value):
                raise ValueError(f"{field} {value!r} is not semver")

        step_ids = [s.id for s in self.steps]
        dupes = {i for i in step_ids if step_ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        known_steps = set(step_ids)

        input_names = {p.name for p in self.inputs}
        secret_names = {s.name for s in self.secrets}
        extract_names = {e.name for e in self.extract}
        output_names = {o.name for o in self.outputs}

        for step in self.steps:
            v = step.value
            if v is None:
                continue
            if v.from_input and v.from_input not in input_names:
                raise ValueError(
                    f"step {step.id}: undeclared input {v.from_input!r}"
                )
            if v.from_secret and v.from_secret not in secret_names:
                raise ValueError(
                    f"step {step.id}: undeclared secret {v.from_secret!r}"
                )

        for secret in self.secrets:
            unknown = set(secret.used_in_steps) - known_steps
            if unknown:
                raise ValueError(
                    f"secret {secret.name!r} names unknown steps {sorted(unknown)}"
                )

        for cp in self.checkpoints:
            if cp.after_step not in known_steps:
                raise ValueError(f"checkpoint after unknown step {cp.after_step!r}")
            self._check_detectors(cp.assertions, extract_names, f"checkpoint {cp.after_step}")

        for cond in self.conditions:
            if isinstance(cond.applies_to, list):
                unknown = set(cond.applies_to) - known_steps
                if unknown:
                    raise ValueError(
                        f"condition {cond.code}: unknown steps {sorted(unknown)}"
                    )
            self._check_detectors([cond.detect], extract_names, f"condition {cond.code}")

        missing_outputs = output_names - extract_names
        if missing_outputs:
            raise ValueError(
                f"declared outputs with nothing to extract them: {sorted(missing_outputs)}"
            )

        if len(extract_names) != len(self.extract):
            raise ValueError("duplicate extraction names")

        self._check_placeholders(input_names)
        return self

    def _check_placeholders(self, input_names: set[str]) -> None:
        """Every `${name}` in a target or url must name a declared input or a
        known runtime variable. Catching this at load time means a capability
        cannot fail halfway through a flow because a placeholder had nothing to
        fill it - by which point it may already have acted."""
        from src.artifact.binding import RUNTIME_VARS, template_vars

        allowed = input_names | set(RUNTIME_VARS)
        sources: list[tuple[str, object]] = []
        for s in self.steps:
            sources.append((f"step {s.id}", s.target))
            if s.url:
                sources.append((f"step {s.id} url", s.url))
        sources += [(f"extraction {e.name}", e.target) for e in self.extract]
        sources.append(("entry", self.app_profile.entry))

        for where, obj in sources:
            unknown = template_vars(obj) - allowed
            if unknown:
                raise ValueError(
                    f"{where}: references {sorted(unknown)}, which is neither a "
                    f"declared input nor a runtime variable"
                )

    @staticmethod
    def _check_detectors(detectors, extract_names: set[str], where: str) -> None:
        for d in detectors:
            if d.kind == "matches" and d.extract not in extract_names:
                raise ValueError(
                    f"{where}: asserts on {d.extract!r}, which is not extracted"
                )

    # ------------------------------------------------------------ agent-facing

    def input_schema(self) -> dict:
        """JSON Schema for the inputs, for a calling agent's tool definition."""
        props: dict[str, dict] = {}
        for p in self.inputs:
            spec: dict = {"type": p.type}
            if p.pattern:
                spec["pattern"] = p.pattern
            if p.enum:
                spec["enum"] = p.enum
            if p.description:
                spec["description"] = p.description
            props[p.name] = spec
        return {
            "type": "object",
            "properties": props,
            "required": [p.name for p in self.inputs if p.required],
            "additionalProperties": False,
        }

    def output_schema(self) -> dict:
        props: dict[str, dict] = {}
        for o in self.outputs:
            spec: dict = {"type": o.type}
            if o.pattern:
                spec["pattern"] = o.pattern
            if o.description:
                spec["description"] = o.description
            props[o.name] = spec
        return {
            "type": "object",
            "properties": props,
            "required": [o.name for o in self.outputs if o.required],
            "additionalProperties": False,
        }

    def sensitive_inputs(self) -> set[str]:
        return {p.name for p in self.inputs if p.sensitivity != "none"}

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    def conditions_for(self, step_id: str) -> list[Condition]:
        return [
            c for c in self.conditions
            if c.applies_to == "any" or step_id in c.applies_to
        ]
