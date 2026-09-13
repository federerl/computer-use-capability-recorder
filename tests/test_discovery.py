"""The discovery loop and the compiler, driven by a scripted model."""

from __future__ import annotations

import json
import re

import pytest

from src.artifact.compile import CompileRequest, compile_capability
from src.artifact.schema import AppProfile, Capability, ParamSpec, SecretRef
from src.discovery.agent import DiscoveryConfig, DiscoveryRun
from src.discovery.tools import ToolCallError, tool_definitions, validate_value_call
from src.evidence.log import RunLog
from src.safety.redaction import Redactor
from src.surface.web_playwright import WebSurface
from tests.fakes import FakeModel, Move, Select

INPUTS = {
    "member_id": "100482",
    "account_kind": "Checking",
    "check_number": "1043",
    "reason": "lost_check",
}
SECRETS = {"operator_user": "env:BACKOFFICE_USER",
           "operator_password": "env:BACKOFFICE_PASSWORD"}

INPUT_SPECS = [
    ParamSpec(name="member_id", pattern="^[0-9]{6}$", sensitivity="pii_id",
              example="100482"),
    ParamSpec(name="account_kind", enum=["Checking", "Savings"], example="Checking"),
    ParamSpec(name="check_number", pattern="^[0-9]{1,6}$", example="1043"),
    ParamSpec(name="reason", enum=["lost_check", "stolen_check"], example="lost_check"),
]
SECRET_SPECS = [SecretRef(name=n, ref=r) for n, r in SECRETS.items()]


def happy_path() -> list[Move]:
    return [
        Move("fill", Select("textbox", "Operator ID"),
             {"source": "secret", "name": "operator_user"}),
        Move("fill", Select("textbox", "Password"),
             {"source": "secret", "name": "operator_password"}),
        Move("click", Select("button", "Sign On")),
        Move("fill", Select("textbox", "Member Number"),
             {"source": "input", "name": "member_id"}),
        Move("click", Select("button", "Search")),
        Move("click", Select("link", "View", under="Checking")),
        Move("click", Select("link", "Accounts", frame="detail")),
        Move("click", Select("link", "Stop Pay", under="Checking", frame="detail")),
        Move("fill", Select("textbox", "", under="Check Number", frame="detail"),
             {"source": "input", "name": "check_number"}),
        Move("select", Select("combobox", "", under="Reason", frame="detail"),
             {"source": "input", "name": "reason"}),
        Move("click", Select("button", "Submit Stop Payment", frame="detail")),
    ]


def finalize_move() -> Move:
    return Move("finalize_capability", None, {
        "title": "Place a stop payment on a check",
        "description": "Looks up a member, opens an account and places a stop payment.",
        "success_text": "Stop Payment Confirmed",
        "success_frame": "detail",
        "outputs": [
            {"name": "confirmation_number", "ref": "__conf__", "type": "string",
             "pattern": "^SP-[0-9]{8}$", "description": "Servicing reference."},
            {"name": "stop_payment_fee", "ref": "__fee__", "type": "number",
             "pattern": None, "description": "Fee charged."},
        ],
        "anticipated_conditions": [
            {"code": "MEMBER_NOT_FOUND", "text": "No records match", "frame": "main",
             "outcome": "member_not_found", "message": "No such member."},
        ],
    })


class _FinalizeModel(FakeModel):
    """Resolves the output refs against the confirmation screen it is shown."""

    def _next(self, kwargs):
        move = self.moves[self._cursor] if self._cursor < len(self.moves) else None
        if move is not None and move.tool == "finalize_capability":
            from tests.fakes import find_ref
            inventory = self._inventory(kwargs)
            labels = {"__conf__": "Confirmation Number", "__fee__": "Stop Payment Fee"}
            for spec in move.args["outputs"]:
                # Only placeholders are resolved; a ref written by a test on
                # purpose is left exactly as written.
                label = labels.get(spec["ref"])
                if label:
                    spec["ref"] = find_ref(inventory, "cell", under=label, index=1)
        return super()._next(kwargs)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("BACKOFFICE_USER", "roper")
    monkeypatch.setenv("BACKOFFICE_PASSWORD", "meridian-demo-pw")


@pytest.fixture()
def run_bits(page, live_app, tmp_path, env):
    log = RunLog(tmp_path / "runs", "disc-test", Redactor())
    surface = WebSurface(page)
    return surface, log


def discover(surface, log, live_app, moves, **overrides):
    run = DiscoveryRun(
        surface, _FinalizeModel(moves), log,
        goal="Place a stop payment on check 1043 for member 100482.",
        entry=f"{live_app}/login", inputs=INPUTS, secrets=SECRETS,
        config=DiscoveryConfig(max_steps=overrides.get("max_steps", 20),
                               screenshot_every_step=False),
    )
    return run.run()


# --------------------------------------------------------------- tool schemas

def test_tools_name_the_declared_bindings():
    tools = tool_definitions(["member_id"], ["operator_password"])
    fill = next(t for t in tools if t["name"] == "fill")
    assert fill["strict"] is True
    assert fill["input_schema"]["additionalProperties"] is False
    assert "member_id" in fill["input_schema"]["properties"]["name"]["description"]
    assert "operator_password" in fill["input_schema"]["properties"]["name"]["description"]


def test_undeclared_bindings_are_rejected():
    with pytest.raises(ToolCallError, match="not a declared input"):
        validate_value_call({"source": "input", "name": "ssn"}, ["member_id"], [])
    with pytest.raises(ToolCallError, match="not a declared secret"):
        validate_value_call({"source": "secret", "name": "pin"}, [], ["password"])


# ------------------------------------------------------------------ the loop

def test_run_completes_and_records_bindings(run_bits, live_app):
    surface, log = run_bits
    result = discover(surface, log, live_app, [*happy_path(), finalize_move()])

    assert result.status == "completed", result.detail
    assert "Stop Payment Confirmed" in surface.text("detail")

    by_id = {s.id: s for s in result.steps}
    member = next(s for s in result.steps if s.value and s.value.from_input == "member_id")
    assert member.action == "fill"
    assert member.value.literal is None, "a supplied value must not be recorded literally"

    secrets_used = {s.value.from_secret for s in result.steps
                    if s.value and s.value.from_secret}
    assert secrets_used == {"operator_user", "operator_password"}
    assert by_id["s1"].action == "navigate"


def test_secret_values_never_reach_the_log(run_bits, live_app):
    surface, log = run_bits
    discover(surface, log, live_app, [*happy_path(), finalize_move()])

    written = (log.dir / "log.jsonl").read_text(encoding="utf-8")
    assert "meridian-demo-pw" not in written
    assert "<<secret:operator_password>>" in written
    assert "100482" in written, "redaction must not swallow the identifiers that " \
                               "make a log useful"


def test_derived_waits_beat_generic_quiescence(run_bits, live_app):
    surface, log = run_bits
    result = discover(surface, log, live_app, [*happy_path(), finalize_move()])

    waits = [s.wait for s in result.steps if s.wait.until == "text"]
    assert waits, "no step learned a completion signal from what it changed"
    assert any(w.text == "Stop Payment Confirmed" for w in waits)


def test_a_bad_ref_is_corrected_not_fatal(run_bits, live_app):
    """A mistyped ref is something the model can fix; ending the run would throw
    away every step already spent."""
    surface, log = run_bits
    moves = [Move("click", None, {"ref": "e999"}), *happy_path(), finalize_move()]
    result = discover(surface, log, live_app, moves)

    assert result.status == "completed"
    kinds = [e["kind"] for e in log.events()]
    assert "tool_rejected" in kinds


def test_run_stops_at_the_step_limit(run_bits, live_app):
    surface, log = run_bits
    # Signing on with nothing filled in re-renders the same screen, so the loop
    # can spin without the script running out of controls to point at.
    result = discover(surface, log, live_app,
                      [Move("click", Select("button", "Sign On"))] * 8, max_steps=4)

    assert result.status == "stopped_max_steps"
    assert log.events()[-1]["kind"] == "run_finished", \
        "a bounded stop must still leave evidence"


def test_give_up_is_a_clean_stop(run_bits, live_app):
    surface, log = run_bits
    result = discover(surface, log, live_app,
                      [Move("give_up", None, {"reason": "two identical rows"})])

    assert result.status == "gave_up"
    assert "identical rows" in result.detail


def test_outputs_must_be_locatable_without_their_value(run_bits, live_app):
    """An output pointed at a bare value would bake this run's answer into the
    artifact, so the loop refuses it and says so."""
    surface, log = run_bits
    bad = finalize_move()
    bad.args["outputs"] = [{"name": "x", "ref": "e1", "type": "string",
                            "pattern": None, "description": ""}]
    result = discover(surface, log, live_app,
                      [*happy_path(), bad, Move("give_up", None, {"reason": "stop"})])

    assert result.status == "gave_up"
    assert any(e["kind"] == "tool_call" and e["tool"] == "finalize_capability"
               for e in log.events())


# ------------------------------------------------------------------ compiling

@pytest.fixture()
def compiled(run_bits, live_app):
    surface, log = run_bits
    result = discover(surface, log, live_app, [*happy_path(), finalize_move()])
    assert result.ok, result.detail
    return compile_capability(CompileRequest(
        capability_id="meridian.stop_payment.place",
        result=result, inputs=INPUT_SPECS, secrets=SECRET_SPECS,
        app_profile=AppProfile(product="meridian-backoffice", tenant="meridian-cu",
                               entry="${BASE_URL}/login"),
        base_url=live_app, model="fake", trace_ref=str(log.dir / "log.jsonl"),
    )), log


def test_compiled_artifact_is_valid(compiled):
    cap, _ = compiled
    assert isinstance(cap, Capability)
    assert cap.approval_state == "draft", "a discovered capability is never pre-approved"
    assert cap.provenance.discovered_by.run_id == "disc-test"
    assert cap.steps[0].url == "${BASE_URL}/login", "the host must not be baked in"


def test_values_are_bound_and_secrets_only_referenced(compiled):
    cap, _ = compiled
    bindings = {s.value.from_input for s in cap.steps if s.value and s.value.from_input}
    assert "member_id" in bindings and "check_number" in bindings

    raw = json.dumps(cap.model_dump(mode="json"))
    assert "meridian-demo-pw" not in raw
    assert all(s.ref.startswith("env:") for s in cap.secrets)
    assert {s.name for s in cap.secrets} == {"operator_user", "operator_password"}


def test_a_target_that_is_an_input_becomes_a_placeholder(compiled):
    """The account row was chosen by a value that is an input, so the recorded
    target generalises to any account."""
    cap, _ = compiled
    anchors = [s.target.primary.scope_anchor for s in cap.steps
               if s.target and getattr(s.target.primary, "scope_anchor", None)]
    assert "${account_kind}" in anchors


def test_a_target_that_merely_contains_an_input_is_flagged_not_rewritten(compiled):
    """The results row carries the member number alongside that member's other
    details. Substituting would produce a target that looks parameterised and
    works for exactly one record."""
    cap, _ = compiled
    assert any("member_id" in note and "will not generalise" in note
               for note in cap.review), cap.review


def test_extractions_are_positional_not_named_by_their_value(compiled):
    cap, _ = compiled
    conf = next(e for e in cap.extract if e.name == "confirmation_number")
    assert conf.target.primary.by == "scoped_role"
    assert conf.target.primary.scope_anchor == "Confirmation Number"
    assert conf.target.disambiguation.nth == 1

    raw = json.dumps(cap.model_dump(mode="json"))
    assert not re.search(r"SP-\d{8}", raw), \
        "this run's confirmation number leaked into the artifact"
    assert "^SP-[0-9]{8}$" in raw, "the expected shape should still be recorded"


def test_checkpoint_asserts_shape_as_well_as_text(compiled):
    cap, _ = compiled
    kinds = {a.kind for a in cap.checkpoints[0].assertions}
    assert kinds == {"text_present", "matches"}


def test_review_notes_say_what_was_not_observed(compiled):
    cap, _ = compiled
    assert any("Recoverable conditions cannot be discovered" in n for n in cap.review)


def test_policy_classifies_risk_that_the_run_cannot_see(page, live_app, tmp_path, env):
    """A run watches a click succeed. It has no way to see that the click
    committed something that cannot be undone, and the discovered draft recorded
    exactly that mistake. The policy that permitted the action does know, so the
    classification comes from there and lands in the recording."""
    from src.safety.policy import Policy, PolicyGate

    policy = Policy.load("policy/discovery.yaml")
    surface = WebSurface(page, gate=PolicyGate(policy),
                         mask_rules=policy.redaction.screenshot_mask)
    log = RunLog(tmp_path / "runs", "disc-risk", Redactor())

    result = discover(surface, log, live_app, [*happy_path(), finalize_move()])
    assert result.ok, result.detail

    cap = compile_capability(CompileRequest(
        capability_id="meridian.stop_payment.place", result=result,
        inputs=INPUT_SPECS, secrets=SECRET_SPECS,
        app_profile=AppProfile(product="meridian-backoffice", tenant="meridian-cu",
                               entry="${BASE_URL}/login"),
        base_url=live_app, model="fake"))

    submit = next(s for s in cap.steps if s.risk == "irreversible")
    assert submit.policy and submit.policy.requires == "confirmation"
    assert any("classified" in note and submit.id in note for note in cap.review)


def test_compiling_an_unfinished_run_is_refused(run_bits, live_app):
    surface, log = run_bits
    result = discover(surface, log, live_app,
                      [Move("give_up", None, {"reason": "stuck"})])
    with pytest.raises(ValueError, match="no successful flow"):
        compile_capability(CompileRequest(
            capability_id="x.y", result=result, inputs=[], secrets=[],
            app_profile=AppProfile(product="p", tenant="t", entry="e"),
            base_url=live_app, model="fake",
        ))
