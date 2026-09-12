"""Deterministic replay: the behaviours the design turns on.

These are the checks worth protecting. Each one corresponds to a claim the
system makes - that replay consults no model, that a business outcome is an
answer rather than a crash, that an unrecognised state never passes as success,
that a failure says enough to fix it.
"""

from __future__ import annotations

import json

import pytest

from app import server as target
from src.artifact import store
from src.evidence.log import RunLog
from src.replay.engine import Escalation, InputError, ReplayEngine, validate_inputs
from src.replay.result import NoLLM, ReplayPurityError
from src.safety.redaction import Redactor
from src.surface.web_playwright import WebSurface

ARTIFACT = "capabilities/meridian.stop_payment.place.json"

RECORDED = {"member_id": "100482", "account_kind": "Checking",
            "check_number": "1043", "reason": "lost_check"}
DIFFERENT = {"member_id": "100517", "account_kind": "Checking",
             "check_number": "2210", "reason": "stolen_check"}


@pytest.fixture()
def capability():
    return store.load(ARTIFACT)


@pytest.fixture()
def engine_for(page, live_app, tmp_path, monkeypatch, capability):
    monkeypatch.setenv("BACKOFFICE_USER", "roper")
    monkeypatch.setenv("BACKOFFICE_PASSWORD", "meridian-demo-pw")

    def build(**kwargs):
        log = RunLog(tmp_path / "runs", f"replay-{len(list(tmp_path.glob('runs/*')))}",
                     Redactor())
        engine = ReplayEngine(WebSurface(page), capability, log,
                              runtime={"BASE_URL": live_app},
                              auto_confirm=kwargs.pop("auto_confirm", True),
                              **kwargs)
        return engine, log

    return build


def arm(fault: str) -> None:
    target.FAULTS[fault] = True


# ------------------------------------------------------------------- success

def test_replay_succeeds_on_the_recorded_inputs(engine_for):
    engine, _ = engine_for()
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    assert result.outputs["confirmation_number"].startswith("SP-")
    assert result.outputs["masked_account_number"] == "****4821"
    assert result.outputs["check_number_confirmed"] == "1043"
    assert result.exit_code == 0


def test_replay_generalises_to_different_inputs(engine_for):
    """A different member, a different account, a different reason. Same
    artifact, same steps, different answers."""
    engine, _ = engine_for()
    result = engine.run(DIFFERENT)

    assert result.status == "success", result.error
    assert result.outputs["masked_account_number"] == "****7715"
    assert result.outputs["check_number_confirmed"] == "2210"


def test_replay_consults_no_model(engine_for):
    engine, log = engine_for()
    result = engine.run(RECORDED)

    assert result.llm_calls == 0
    assert isinstance(engine.llm, NoLLM)
    with pytest.raises(ReplayPurityError, match="makes no model calls"):
        engine.llm.messages.create(model="claude-opus-5")

    events = {e["kind"] for e in log.events()}
    assert "model_call" not in events


def test_outputs_are_shaped_as_declared(engine_for, capability):
    engine, _ = engine_for()
    result = engine.run(RECORDED)

    import re
    for spec in capability.outputs:
        value = result.outputs[spec.name]
        if spec.pattern:
            assert re.fullmatch(spec.pattern, str(value)), \
                f"{spec.name}={value!r} does not match {spec.pattern}"


# ---------------------------------------------------------- business outcomes

@pytest.mark.parametrize("inputs,code,outcome", [
    ({**RECORDED, "member_id": "999999"}, "MEMBER_NOT_FOUND", "member_not_found"),
    ({**RECORDED, "check_number": "1009"}, "CHECK_ALREADY_CLEARED", "check_already_cleared"),
    ({**RECORDED, "member_id": "100633", "check_number": "3001"},
     "ACCOUNT_RESTRICTED", "account_restricted"),
])
def test_business_outcomes_are_answers_not_crashes(engine_for, inputs, code, outcome):
    engine, _ = engine_for()
    result = engine.run(inputs)

    assert result.status == "business_outcome", result.error
    assert result.outcome.code == code
    assert result.outcome.outcome == outcome
    assert result.outcome.message, "a caller needs a sentence, not just a code"
    assert result.error is None, "a legitimate answer must not arrive as an error"
    assert result.exit_code == 3, "and must be distinguishable from failure by a caller"


# ------------------------------------------------------------------ recovery

def test_interstitial_is_dismissed_and_the_run_continues(engine_for):
    engine, _ = engine_for()
    arm("system_notice")
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    recovered = [r for r in result.recoveries if r.code == "SYSTEM_NOTICE"]
    assert recovered and recovered[0].resolved
    assert recovered[0].then == "continue", (
        "the interstitial was found after the step had already run, so the "
        "recovery must clear it and carry on rather than repeat the action"
    )


def test_session_timeout_signs_back_in_and_restarts(engine_for):
    engine, _ = engine_for()
    arm("session_expired")
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    recovered = [r for r in result.recoveries if r.code == "SESSION_EXPIRED"]
    assert recovered and recovered[0].then == "restart_flow"


def test_restart_is_refused_once_something_irreversible_has_run(engine_for):
    """Re-running the flow after the submit would place a second stop payment.
    The engine hands the decision to a person rather than repeating it."""
    engine, _ = engine_for()
    engine._irreversible_done = True
    arm("session_expired")

    result = engine.run(RECORDED)

    assert result.status == "escalated"
    assert result.error.code == "RESTART_WOULD_REPEAT_IRREVERSIBLE"
    assert result.exit_code == 4


# ------------------------------------------------------------- hard failures

def test_a_relabelled_control_is_still_found_by_a_fallback(engine_for):
    """The submit button is renamed. Role and name no longer match, but the
    control is the same one in the same place, and the recorded selector finds
    it. This is what the ranked list is for."""
    engine, _ = engine_for()
    arm("relabel_submit")
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    submit = next(s for s in result.steps if s.step_id == "s12")
    assert submit.strategy == "css"
    assert any("role_name" in a and "0 match" in a for a in submit.attempts), \
        "the preferred strategy should be recorded as having been tried and missed"


def test_a_missing_control_fails_with_what_was_tried(engine_for, capability):
    """When every strategy is exhausted, replay stops and says what it looked
    for. It does not fall back on anything resembling a guess."""
    submit = capability.step("s12")
    submit.target.primary.name = "Authorise Disbursement"
    submit.target.fallbacks = []

    engine, _ = engine_for()
    result = engine.run(RECORDED)

    assert result.status == "failed"
    assert result.error.code == "TARGET_NOT_FOUND"
    assert result.error.step_id == "s12"
    assert result.error.locator_attempts, "a failure must say what it looked for"
    assert any("Authorise Disbursement" in a for a in result.error.locator_attempts)
    assert result.error.evidence.get("screenshot")
    assert result.error.evidence.get("snapshot")


def test_landing_somewhere_else_fails_the_checkpoint(engine_for):
    """The click worked and the page settled. Without a checkpoint this would
    have been reported as success."""
    engine, _ = engine_for()
    arm("wrong_confirmation")
    result = engine.run(RECORDED)

    assert result.status == "failed"
    assert result.error.code == "CHECKPOINT_FAILED"
    assert "Stop Payment Confirmed" in result.error.expected
    assert "queued for overnight processing" in result.error.observed


def test_an_unrecognised_state_never_passes_as_success(engine_for):
    engine, _ = engine_for()
    arm("security_challenge")
    result = engine.run(RECORDED)

    assert result.status != "success"
    assert result.outputs is None


# -------------------------------------------------------------------- policy

def test_an_irreversible_step_is_not_taken_unattended(engine_for):
    engine, _ = engine_for(auto_confirm=False)
    result = engine.run(RECORDED)

    assert result.status == "failed"
    assert result.error.code == "CONFIRMATION_REQUIRED"
    assert result.error.step_id == "s12"


def test_confirmation_required_escalates_when_a_person_is_available(engine_for):
    engine, _ = engine_for(auto_confirm=False, allow_escalation=True)
    result = engine.run(RECORDED)

    assert result.status == "escalated"
    assert result.error.code == "CONFIRMATION_REQUIRED"
    assert result.exit_code == 4


# -------------------------------------------------------------------- inputs

def test_bad_inputs_are_rejected_before_anything_is_touched(capability):
    with pytest.raises(InputError, match="must match"):
        validate_inputs(capability, {**RECORDED, "member_id": "12"})
    with pytest.raises(InputError, match="must be one of"):
        validate_inputs(capability, {**RECORDED, "reason": "because"})
    with pytest.raises(InputError, match="missing required"):
        validate_inputs(capability, {"member_id": "100482"})
    with pytest.raises(InputError, match="unknown inputs"):
        validate_inputs(capability, {**RECORDED, "ssn": "412-88-7390"})


def test_inputs_are_correlated_by_digest_not_by_value(engine_for):
    engine, log = engine_for()
    engine.run({**RECORDED, "member_id": "999999"})

    written = (log.dir / "result.json").read_text(encoding="utf-8")
    payload = json.loads(written)
    assert len(payload["inputs_digest"]) == 16
    assert "999999" not in payload["inputs_digest"]


def test_secrets_never_reach_the_replay_evidence(engine_for):
    engine, log = engine_for()
    engine.run(RECORDED)

    written = (log.dir / "log.jsonl").read_text(encoding="utf-8")
    assert "meridian-demo-pw" not in written


# --------------------------------------------------------------- the artifact

def test_the_approved_capability_records_what_review_changed(capability):
    assert capability.approval_state == "approved"
    assert capability.version == "1.1.0"
    assert not capability.review, "an approved capability has nothing outstanding"
    assert len(capability.provenance.human_edits) >= 4
    assert capability.provenance.discovered_by.model == "claude-opus-5"


def test_positional_resolution_stayed_confined_to_one_step(capability):
    positional = [s.id for s in capability.steps
                  if s.target and not s.target.disambiguation.expect_unique]
    assert positional == ["s7"]
    assert capability.step("s7").note
