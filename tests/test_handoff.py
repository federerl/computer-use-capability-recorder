"""Pausing a run, letting a person work the same session, and taking it back."""

from __future__ import annotations

import json
import threading
import time

import pytest

from src.artifact import store
from src.escalation.control import (
    ControlLease, ControlState, ControlViolation, InvalidTransition,
)
from src.escalation.intervention import Intervention
from src.escalation.supervisor import HumanSupervisor
from src.evidence.log import RunLog
from src.replay.engine import ReplayEngine
from src.safety.policy import Policy, PolicyGate
from src.safety.redaction import Redactor
from src.surface.base import ClickAction, FrameScope, NavigateAction, RoleNameStrategy, Target
from src.surface.web_playwright import WebSurface

RECORDED = {"member_id": "100482", "account_kind": "Checking",
            "check_number": "1043", "reason": "lost_check"}


@pytest.fixture()
def capability():
    return store.load("capabilities/meridian.stop_payment.place.json")


@pytest.fixture()
def attended(page, live_app, tmp_path, monkeypatch, capability):
    """A replay wired for handover, plus the operator's view of the same lease."""
    monkeypatch.setenv("BACKOFFICE_USER", "roper")
    monkeypatch.setenv("BACKOFFICE_PASSWORD", "meridian-demo-pw")

    log = RunLog(tmp_path / "runs", "attended", Redactor())
    policy = Policy.load("policy/attended.yaml")
    lease = ControlLease(log.dir / "control.json", run_id="attended")
    surface = WebSurface(page, gate=PolicyGate(policy),
                         mask_rules=policy.redaction.screenshot_mask, lease=lease)

    def build(operator_script, timeout_s=20.0):
        supervisor = HumanSupervisor(lease, log, timeout_s=timeout_s, poll_s=0.05,
                                     on_wait=operator_script)
        engine = ReplayEngine(surface, capability, log,
                              runtime={"BASE_URL": live_app}, supervisor=supervisor)
        return engine, supervisor

    return build, lease, log, surface


# ----------------------------------------------------------------- the lease

def test_only_legal_handovers_are_recorded(tmp_path):
    lease = ControlLease(tmp_path / "control.json", run_id="r")
    assert lease.read().control is ControlState.AUTOMATION

    with pytest.raises(InvalidTransition):
        lease.transition(ControlState.HUMAN, holder="human")

    lease.transition(ControlState.HANDOFF_REQUESTED, holder="automation")
    lease.transition(ControlState.HUMAN, holder="human", operator="dana")
    with pytest.raises(InvalidTransition):
        lease.transition(ControlState.AUTOMATION, holder="automation")

    lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                     resume_mode="approve")
    lease.transition(ControlState.AUTOMATION, holder="automation")
    assert lease.read().seq == 4, "every handover is numbered"


def test_automation_cannot_act_while_a_person_holds_the_session(tmp_path):
    lease = ControlLease(tmp_path / "control.json", run_id="r")
    lease.assert_automation_holds()

    lease.transition(ControlState.HANDOFF_REQUESTED, holder="automation")
    lease.transition(ControlState.HUMAN, holder="human")

    with pytest.raises(ControlViolation, match="while control is 'human'"):
        lease.assert_automation_holds()


def test_a_stale_view_of_control_is_refused(tmp_path):
    """The sequence number is a fencing token: acting against a lease that has
    moved since the step was planned is refused even if control came back."""
    lease = ControlLease(tmp_path / "control.json", run_id="r")
    seq = lease.read().seq

    lease.transition(ControlState.HANDOFF_REQUESTED, holder="automation")
    lease.transition(ControlState.HUMAN, holder="human")
    lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                     resume_mode="approve")
    lease.transition(ControlState.AUTOMATION, holder="automation")

    lease.assert_automation_holds()
    with pytest.raises(ControlViolation, match="control changed underneath"):
        lease.assert_automation_holds(expect_seq=seq)


def test_the_surface_refuses_to_act_without_the_lease(page, live_app, tmp_path):
    lease = ControlLease(tmp_path / "control.json", run_id="r")
    surface = WebSurface(page, lease=lease)
    surface.act(NavigateAction(url=f"{live_app}/login"))

    lease.transition(ControlState.HANDOFF_REQUESTED, holder="automation")
    lease.transition(ControlState.HUMAN, holder="human")

    with pytest.raises(ControlViolation):
        surface.act(NavigateAction(url=f"{live_app}/members"))


# --------------------------------------------------------- the full handover

def sign_off(lease, *, completed: bool, operator="dana", note=""):
    """Stand in for a person running `src.cli operator resume`."""
    def script(current):
        if current.control is ControlState.HUMAN:
            lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                             operator=operator, note=note,
                             resume_mode="completed" if completed else "approve")
    return script


def test_operator_approves_and_automation_performs_the_step(attended):
    build, lease, log, _ = attended
    engine, supervisor = build(sign_off(lease, completed=False, note="checked the fee"))

    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    assert result.outputs["confirmation_number"].startswith("SP-")
    assert result.control["human_intervened"] is True
    assert result.control["verdict"] == "approved"
    assert result.control["operator"] == "dana"
    assert result.control["note"] == "checked the fee"

    assert lease.read().control is ControlState.AUTOMATION
    assert lease.read().seq >= 4


def test_operator_does_it_by_hand_and_automation_does_not_repeat_it(attended, live_app):
    """The distinction that matters: an approved irreversible step performed
    twice is two stop payments."""
    build, lease, log, surface = attended

    def operator_does_the_work(current):
        if current.control is not ControlState.HUMAN:
            return
        # The same live session - the person is looking at the form.
        surface.frame("detail").get_by_role(
            "button", name="Submit Stop Payment").click()
        surface._settle()
        lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                         operator="dana", resume_mode="completed")

    engine, _ = build(operator_does_the_work)
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    assert result.control["verdict"] == "completed"

    performed = next(s for s in result.steps if s.step_id == "s12")
    assert performed.strategy == "human"
    assert "performed by dana" in performed.detail

    # One stop payment, not two.
    from app.seed import MEMBERS
    checks = MEMBERS["100482"].account("Checking").checks
    assert [c.status for c in checks if c.number == "1043"] == ["stopped"]


def test_the_operator_actions_are_recorded(attended, live_app):
    build, lease, log, surface = attended

    def operator_does_the_work(current):
        if current.control is not ControlState.HUMAN:
            return
        surface.frame("detail").get_by_role(
            "button", name="Submit Stop Payment").click()
        surface._settle()
        lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                         operator="dana", resume_mode="completed")

    engine, _ = build(operator_does_the_work)
    engine.run(RECORDED)

    recorded = (log.dir / "human_actions.jsonl").read_text(encoding="utf-8").splitlines()
    assert recorded, "a handover with no record of what was done is a hole in the trail"
    actions = [json.loads(line) for line in recorded]
    assert any(a["kind"] == "click" and "Submit Stop Payment" in (a["label"] or "")
               for a in actions)
    assert any(e["kind"] == "human_action" for e in log.events())


def test_wandering_off_is_caught_when_control_comes_back(attended, live_app):
    """A person with a live browser can go anywhere. Carrying on because the
    lease says it is our turn would act on a page nobody checked."""
    build, lease, log, surface = attended

    def operator_wanders_off(current):
        if current.control is not ControlState.HUMAN:
            return
        surface.page.goto(f"{live_app}/members")
        surface._settle()
        lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                         operator="dana", resume_mode="approve")

    engine, _ = build(operator_wanders_off)
    result = engine.run(RECORDED)

    assert result.status == "failed"
    assert result.error.code == "POST_HANDOFF_STATE_MISMATCH"
    assert "no longer resolvable" in result.error.observed


def test_claiming_a_step_was_done_when_it_was_not_is_caught(attended, live_app):
    build, lease, log, surface = attended

    def operator_claims_without_doing(current):
        if current.control is ControlState.HUMAN:
            lease.transition(ControlState.RESUME_REQUESTED, holder="human",
                             operator="dana", resume_mode="completed")

    engine, _ = build(operator_claims_without_doing)
    result = engine.run(RECORDED)

    assert result.status == "failed"
    assert result.error.code == "POST_HANDOFF_STATE_MISMATCH"
    assert "reported the step done" in result.error.observed


def test_the_operator_can_end_the_run(attended):
    build, lease, log, _ = attended

    def operator_aborts(current):
        if current.control is ControlState.HUMAN:
            lease.transition(ControlState.ABORTED, holder="human", operator="dana",
                             note="wrong member")

    engine, _ = build(operator_aborts)
    result = engine.run(RECORDED)

    assert result.status == "escalated"
    assert result.exit_code == 4
    assert result.control["verdict"] == "aborted"


def test_nobody_comes_and_the_run_does_not_proceed_on_its_own(attended):
    build, lease, log, _ = attended
    engine, _ = build(lambda current: None, timeout_s=1.0)

    result = engine.run(RECORDED)

    assert result.status == "escalated"
    assert "no response" in result.error.observed


# ----------------------------------------------------------- the intervention

def test_the_request_carries_enough_to_act_on(attended):
    build, lease, log, _ = attended
    engine, _ = build(sign_off(lease, completed=False))
    engine.run(RECORDED)

    intervention = Intervention.read(log.dir)
    assert intervention is not None
    assert intervention.code == "CONFIRMATION_REQUIRED"
    assert intervention.step_id == "s12"
    assert intervention.step_risk == "irreversible"
    assert intervention.capability_id == "meridian.stop_payment.place"
    assert intervention.url
    assert intervention.screenshot and intervention.snapshot
    assert intervention.log_tail
    assert len(intervention.options) == 3

    described = intervention.describe()
    assert "resume --completed" in described
    assert "meridian-demo-pw" not in described


def test_the_intervention_holds_no_raw_identifiers(attended):
    build, lease, log, _ = attended
    engine, _ = build(sign_off(lease, completed=False))
    engine.run(RECORDED)

    raw = (log.dir / "intervention.json").read_text(encoding="utf-8")
    assert "meridian-demo-pw" not in raw
    assert "412-88-7390" not in raw
    assert len(json.loads(raw)["inputs_digest"]) == 16


# ------------------------------------------------------ a separate operator

def test_control_moves_through_the_file_not_the_process(attended):
    """The operator runs elsewhere. The lease file is the whole channel, which
    is why a command line in another terminal is enough."""
    build, lease, log, _ = attended
    released = threading.Event()

    def operator_in_another_process(current):
        if current.control is ControlState.HUMAN and not released.is_set():
            released.set()

            def elsewhere():
                time.sleep(0.1)
                ControlLease(log.dir / "control.json").transition(
                    ControlState.RESUME_REQUESTED, holder="human",
                    operator="remote", resume_mode="approve")

            threading.Thread(target=elsewhere, daemon=True).start()

    engine, _ = build(operator_in_another_process)
    result = engine.run(RECORDED)

    assert result.status == "success", result.error
    assert result.control["operator"] == "remote"


def test_the_lease_survives_a_reader_and_a_writer_at_once(tmp_path):
    """The operator is a separate process, so reads and writes overlap.

    On Windows a file cannot be replaced while anything has it open, so without
    retries on both sides a handover fails intermittently - the worst way for it
    to fail, because it passes every time you look at it.
    """
    import itertools
    import threading
    import time

    lease = ControlLease(tmp_path / "control.json", run_id="race")
    errors: list[tuple[str, str]] = []
    stop = threading.Event()
    cycle = itertools.cycle([
        (ControlState.HANDOFF_REQUESTED, "automation"),
        (ControlState.HUMAN, "human"),
        (ControlState.RESUME_REQUESTED, "human"),
        (ControlState.AUTOMATION, "automation"),
    ])

    def operator_polling():
        while not stop.is_set():
            try:
                lease.read()
            except Exception as exc:
                errors.append(("read", f"{type(exc).__name__}: {exc}"))
                return
            time.sleep(0.01)

    watcher = threading.Thread(target=operator_polling, daemon=True)
    watcher.start()
    try:
        for _ in range(120):
            try:
                lease.transition(*next(cycle))
            except Exception as exc:
                errors.append(("write", f"{type(exc).__name__}: {exc}"))
                break
    finally:
        stop.set()
        watcher.join(timeout=2)

    assert not errors, errors[:3]
    assert lease.read().seq == 120
