"""The artifact contract, and the mistakes it refuses to accept.

Most of these assert a *rejection*. The artifact is the thing a person reviews
and an agent calls, so a capability that is internally inconsistent should fail
to load rather than fail halfway through a flow - by which point it may already
have acted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.artifact import binding, store
from src.artifact.schema import Capability
from src.surface.base import Target

EXAMPLE = Path("schema/example.capability.json")


@pytest.fixture()
def raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def build(raw: dict, **overrides) -> Capability:
    data = copy.deepcopy(raw)
    data.update(overrides)
    return Capability.model_validate(data)


# ------------------------------------------------------------------ the example

def test_example_artifact_is_valid(raw):
    cap = Capability.model_validate(raw)
    assert cap.id == "meridian.stop_payment.place"
    assert len(cap.steps) == 12
    assert {c.klass for c in cap.conditions} == {"business_outcome", "recoverable"}


def test_example_round_trips(raw, tmp_path):
    cap = Capability.model_validate(raw)
    path = store.save(cap, tmp_path / "c.json")
    assert store.load(path).model_dump() == cap.model_dump()


def test_published_json_schema_is_current():
    """The published contract is generated from the models that enforce it, so
    it cannot drift into describing something the code does not do."""
    committed = json.loads(Path("schema/capability.schema.json").read_text(encoding="utf-8"))
    assert committed == store.json_schema(), \
        "run: uv run python -c 'from src.artifact.store import export_json_schema; export_json_schema()'"


def test_agent_facing_schemas(raw):
    cap = Capability.model_validate(raw)
    ins = cap.input_schema()
    assert ins["required"] == ["member_id", "account_kind", "check_number", "reason"]
    assert ins["additionalProperties"] is False
    assert ins["properties"]["reason"]["enum"] == ["lost_check", "stolen_check", "dispute", "other"]
    assert cap.output_schema()["properties"]["confirmation_number"]["pattern"] == "^SP-[0-9]{8}$"


# ------------------------------------------------------- secrets and literals

def test_secret_must_use_a_known_scheme(raw):
    raw["secrets"][0]["ref"] = "vault://prod/operator"
    with pytest.raises(ValidationError, match="env:NAME"):
        Capability.model_validate(raw)


def test_step_cannot_reference_an_undeclared_secret(raw):
    raw["steps"][1]["value"] = {"from_secret": "operator_pin"}
    with pytest.raises(ValidationError, match="undeclared secret"):
        Capability.model_validate(raw)


def test_step_cannot_reference_an_undeclared_input(raw):
    raw["steps"][4]["value"] = {"from_input": "customer_ssn"}
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability.model_validate(raw)


def test_a_value_needs_exactly_one_source(raw):
    raw["steps"][4]["value"] = {"from_input": "member_id", "literal": "100482"}
    with pytest.raises(ValidationError, match="exactly one source"):
        Capability.model_validate(raw)


@pytest.mark.parametrize("literal,what", [
    ("412-88-7390", "social security number"),
    ("4539872210034821", "card number"),
])
def test_regulated_looking_literals_are_refused(raw, literal, what):
    """The schema refuses to store something shaped like regulated data, rather
    than relying on every future author to notice."""
    raw["steps"][4]["value"] = {"literal": literal}
    with pytest.raises(ValidationError, match=what):
        Capability.model_validate(raw)


def test_an_ordinary_literal_is_still_allowed(raw):
    raw["steps"][4]["value"] = {"literal": "100482"}
    assert Capability.model_validate(raw)


# ------------------------------------------------------------- condition shape

def test_business_outcome_cannot_have_a_recovery(raw):
    cond = next(c for c in raw["conditions"] if c["code"] == "MEMBER_NOT_FOUND")
    cond["recover"] = {"steps": [], "then": "continue"}
    with pytest.raises(ValidationError, match="an answer, not something to recover"):
        Capability.model_validate(raw)


def test_business_outcome_must_end_the_run(raw):
    cond = next(c for c in raw["conditions"] if c["code"] == "MEMBER_NOT_FOUND")
    cond["terminal"] = False
    with pytest.raises(ValidationError, match="ends the run"):
        Capability.model_validate(raw)


def test_business_outcome_needs_a_name_to_branch_on(raw):
    cond = next(c for c in raw["conditions"] if c["code"] == "MEMBER_NOT_FOUND")
    del cond["outcome"]
    with pytest.raises(ValidationError, match="needs an outcome name"):
        Capability.model_validate(raw)


def test_recoverable_without_a_recovery_is_refused(raw):
    cond = next(c for c in raw["conditions"] if c["code"] == "SYSTEM_NOTICE")
    del cond["recover"]
    with pytest.raises(ValidationError, match="hard failure with a friendlier name"):
        Capability.model_validate(raw)


def test_condition_cannot_name_an_unknown_step(raw):
    raw["conditions"][0]["applies_to"] = ["s99"]
    with pytest.raises(ValidationError, match="unknown steps"):
        Capability.model_validate(raw)


# ------------------------------------------------------ structural consistency

def test_duplicate_step_ids_are_refused(raw):
    raw["steps"][5]["id"] = "s5"
    with pytest.raises(ValidationError, match="duplicate step ids"):
        Capability.model_validate(raw)


def test_checkpoint_must_follow_a_real_step(raw):
    raw["checkpoints"][0]["after_step"] = "s13"
    with pytest.raises(ValidationError, match="checkpoint after unknown step"):
        Capability.model_validate(raw)


def test_checkpoint_cannot_assert_on_something_never_extracted(raw):
    raw["checkpoints"][0]["assertions"][1]["extract"] = "teller_id"
    with pytest.raises(ValidationError, match="not extracted"):
        Capability.model_validate(raw)


def test_declared_output_needs_an_extraction(raw):
    raw["outputs"].append({"name": "hold_expires_at", "type": "string"})
    with pytest.raises(ValidationError, match="nothing to extract them"):
        Capability.model_validate(raw)


def test_navigate_does_not_take_a_target(raw):
    raw["steps"][0]["target"] = raw["steps"][3]["target"]
    with pytest.raises(ValidationError, match="navigate does not take a target"):
        Capability.model_validate(raw)


def test_fill_needs_a_value(raw):
    del raw["steps"][4]["value"]
    with pytest.raises(ValidationError, match="fill needs a value"):
        Capability.model_validate(raw)


# -------------------------------------------------------------- placeholders

def test_placeholder_must_name_a_declared_input(raw):
    step = next(s for s in raw["steps"] if s["id"] == "s9")
    step["target"]["primary"]["scope_anchor"] = "${product_code}"
    with pytest.raises(ValidationError, match="neither a declared input nor a runtime"):
        Capability.model_validate(raw)


def test_runtime_placeholders_are_allowed(raw):
    assert Capability.model_validate(raw).steps[0].url == "${BASE_URL}/login"


def test_target_binding_substitutes_and_copies(raw):
    cap = Capability.model_validate(raw)
    target = cap.step("s9").target

    bound = binding.bind_target(target, {"account_kind": "Savings"})
    assert bound.primary.scope_anchor == "Savings"
    assert target.primary.scope_anchor == "${account_kind}", \
        "binding must not mutate the loaded capability between runs"


def test_unbound_placeholder_is_reported_not_guessed(raw):
    cap = Capability.model_validate(raw)
    with pytest.raises(binding.UnboundReference, match="account_kind"):
        binding.bind_target(cap.step("s9").target, {})


def test_secret_resolution_reads_the_environment(monkeypatch):
    monkeypatch.setenv("BACKOFFICE_PASSWORD", "hunter2")
    assert binding.read_secret("env:BACKOFFICE_PASSWORD") == "hunter2"
    monkeypatch.delenv("BACKOFFICE_PASSWORD")
    with pytest.raises(binding.UnboundReference):
        binding.read_secret("env:BACKOFFICE_PASSWORD")


# ------------------------------------------------------------ version gating

def test_newer_major_schema_is_refused(raw):
    raw["schema_version"] = "2.0.0"
    with pytest.raises(store.SchemaVersionError, match="refusing to execute"):
        store.loads(json.dumps(raw))


def test_same_major_is_accepted(raw):
    raw["schema_version"] = "1.4.2"
    assert store.loads(json.dumps(raw)).id == "meridian.stop_payment.place"


# ------------------------------------------------------------------ accessors

def test_conditions_for_step_includes_global_ones(raw):
    cap = Capability.model_validate(raw)
    codes = {c.code for c in cap.conditions_for("s6")}
    assert "MEMBER_NOT_FOUND" in codes          # scoped to s6
    assert "SYSTEM_NOTICE" in codes             # applies anywhere
    assert "CHECK_ALREADY_CLEARED" not in codes  # scoped elsewhere


def test_the_only_positional_target_is_the_deliberate_one(raw):
    """Positional resolution is an escape hatch. If it spreads past the one step
    that documents why it is there, the targeting has quietly become guesswork."""
    cap = Capability.model_validate(raw)
    positional = [s.id for s in cap.steps
                  if s.target and not s.target.disambiguation.expect_unique]
    assert positional == ["s7"]
    assert cap.step("s7").note


def test_irreversible_steps_declare_a_policy(raw):
    cap = Capability.model_validate(raw)
    for step in cap.steps:
        if step.risk == "irreversible":
            assert step.policy and step.policy.requires == "confirmation", \
                f"step {step.id} is irreversible but unguarded"
