"""Guardrails: where the automation may go, what it may do, and what it writes down."""

from __future__ import annotations

import re

import pytest

from src.artifact import store
from src.evidence.log import RunLog
from src.replay.engine import ReplayEngine
from src.safety.policy import Policy, PolicyGate, label_of
from src.safety.redaction import Redactor
from src.surface.base import (
    ActionBlocked, ClickAction, FillAction, FrameScope, NavigateAction,
    RoleNameStrategy, Target,
)
from src.surface.web_playwright import WebSurface

ATTENDED = "policy/attended.yaml"
DISCOVERY = "policy/discovery.yaml"
UNATTENDED = "policy/unattended.yaml"

RECORDED = {"member_id": "100482", "account_kind": "Checking",
            "check_number": "1043", "reason": "lost_check"}


def target(role: str, name: str, frame: str = "main") -> Target:
    return Target(scope=FrameScope(frame=frame),
                  primary=RoleNameStrategy(role=role, name=name))


@pytest.fixture()
def gate():
    return PolicyGate(Policy.load(ATTENDED))


# ----------------------------------------------------------------- allowlist

@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:5000/members?f3=100482", True),
    ("http://127.0.0.1:5000/stoppay/new", True),
    ("http://127.0.0.1:5000/_control/fault", False),
    ("http://127.0.0.1:5000/admin/users", False),
    ("http://127.0.0.1:5000/exports/all.csv", False),
    ("https://example.com/members", False),
])
def test_navigation_is_confined_to_the_application(gate, url, allowed):
    decision = gate.check(NavigateAction(url=url), "http://127.0.0.1:5000/members")
    assert decision.allowed is allowed, decision.reason


def test_the_fault_controller_is_out_of_reach(gate):
    """The lever that makes injected states appear must not be reachable by the
    thing those states are meant to test."""
    decision = gate.check(
        NavigateAction(url="http://127.0.0.1:5000/_control/reset"),
        "http://127.0.0.1:5000/members")
    assert decision.verdict == "block"
    assert "denied path" in decision.reason


def test_acting_on_a_page_outside_the_allowlist_is_refused(gate):
    decision = gate.check(ClickAction(target=target("button", "Export")),
                          "https://example.com/anything")
    assert decision.verdict == "block"


def test_landing_somewhere_unexpected_is_caught(gate):
    """A click cannot be vetted in advance; where it went can be."""
    assert gate.check_landing("http://127.0.0.1:5000/members").allowed
    landing = gate.check_landing("https://example.com/")
    assert landing.verdict == "block"
    assert "landed outside policy" in landing.reason


def test_denied_action_types_are_refused(gate):
    from src.surface.base import Action
    decision = gate.check(NavigateAction(url="http://127.0.0.1:5000/members"),
                          "http://127.0.0.1:5000/members")
    assert decision.allowed
    # Action types not on the allow list are refused even inside the app.
    policy = Policy.load(ATTENDED)
    policy.action_types.allow = ["click"]
    assert PolicyGate(policy).check(
        FillAction(target=target("textbox", "Member Number"), value="1"),
        "http://127.0.0.1:5000/members").verdict == "block"


# ------------------------------------------------------------- risky actions

def test_irreversible_actions_are_recognised_by_what_they_say(gate):
    decision = gate.check(target and ClickAction(target=target("button", "Submit Stop Payment")),
                          "http://127.0.0.1:5000/stoppay/new")
    assert decision.verdict == "confirm"
    assert "irreversible" in decision.reason


def test_money_movement_is_blocked_outright(gate):
    decision = gate.check(ClickAction(target=target("button", "Transfer Funds")),
                          "http://127.0.0.1:5000/members")
    assert decision.verdict == "block"


def test_ordinary_actions_are_not_impeded(gate):
    for name in ("Search", "View", "Accounts", "Sign On", "Stop Pay"):
        decision = gate.check(ClickAction(target=target("link", name)),
                              "http://127.0.0.1:5000/members")
        assert decision.verdict == "allow", f"{name}: {decision.reason}"


def test_discovery_flags_rather_than_stopping(gate):
    """A flow that halts before its final step is not worth recording, so
    discovery permits the action and keeps the classification."""
    discovery = PolicyGate(Policy.load(DISCOVERY))
    decision = discovery.check(ClickAction(target=target("button", "Submit Stop Payment")),
                               "http://127.0.0.1:5000/stoppay/new")
    assert decision.verdict == "flag"
    assert decision.allowed
    assert discovery.risk_of(ClickAction(target=target("button", "Submit Stop Payment"))) \
        == "irreversible"


def test_unattended_still_refuses_to_move_money():
    unattended = PolicyGate(Policy.load(UNATTENDED))
    assert unattended.check(ClickAction(target=target("button", "Wire Transfer")),
                            "http://127.0.0.1:5000/members").verdict == "block"
    assert unattended.check(ClickAction(target=target("button", "Submit Stop Payment")),
                            "http://127.0.0.1:5000/stoppay/new").verdict == "flag"


def test_the_label_read_is_the_one_an_operator_would_see():
    assert label_of(ClickAction(target=target("button", "Approve Payment"))) \
        == "Approve Payment"
    assert label_of(NavigateAction(url="http://x/y")) == ""


# ------------------------------------------------------------------ the gate

def test_the_gate_is_the_only_door(page, live_app):
    """No action reaches the browser without passing the gate, including one
    whose destination only becomes known after it happens."""
    policy = Policy.load(ATTENDED)
    policy.allowlist.paths = ["^/login$"]
    surface = WebSurface(page, gate=PolicyGate(policy))

    surface.act(NavigateAction(url=f"{live_app}/login"))
    with pytest.raises(ActionBlocked):
        surface.act(NavigateAction(url=f"{live_app}/members"))


def test_a_capability_that_forgot_to_mark_a_step_is_still_stopped(
        page, live_app, tmp_path, monkeypatch):
    """The gate and the artifact are independent layers. A recording that
    neglects to mark a step is precisely the case a guardrail exists for."""
    monkeypatch.setenv("BACKOFFICE_USER", "roper")
    monkeypatch.setenv("BACKOFFICE_PASSWORD", "meridian-demo-pw")

    capability = store.load("capabilities/meridian.stop_payment.place.json")
    submit = capability.step("s12")
    submit.risk = "safe"
    submit.policy = None

    policy = Policy.load(ATTENDED)
    surface = WebSurface(page, gate=PolicyGate(policy),
                         mask_rules=policy.redaction.screenshot_mask)
    log = RunLog(tmp_path / "runs", "guarded", Redactor())

    result = ReplayEngine(surface, capability, log,
                          runtime={"BASE_URL": live_app},
                          auto_confirm=True).run(RECORDED)

    assert result.status == "failed"
    assert result.error.step_id == "s12"
    assert result.error.code == "CONFIRMATION_REQUIRED"


# ------------------------------------------------------------------ redaction

def test_secrets_are_masked_by_value_and_patterns_by_shape():
    redactor = Redactor()
    redactor.register_secret("operator_password", "meridian-demo-pw")

    text = redactor.text("signing in as roper / meridian-demo-pw, ssn 412-88-7390, "
                         "card 4539872210034821, member 100482")
    assert "meridian-demo-pw" not in text
    assert "<<secret:operator_password>>" in text
    assert "412-88-7390" not in text
    assert "4539872210034821" not in text
    assert "100482" in text, \
        "a rule broad enough to catch member numbers would gut the logs"


def test_short_values_are_not_registered_as_secrets():
    redactor = Redactor()
    redactor.register_secret("pin", "1")
    assert redactor.text("1043") == "1043"


def test_redaction_reaches_into_structures():
    redactor = Redactor()
    redactor.register_secret("pw", "meridian-demo-pw")
    scrubbed = redactor.scrub({"a": ["meridian-demo-pw", {"b": "412-88-7390"}]})
    assert scrubbed == {"a": ["<<secret:pw>>", {"b": "<<redacted:ssn>>"}]}


# ---------------------------------------------------------------- screenshots

def test_sensitive_fields_are_masked_before_the_image_exists(
        page, live_app, tmp_path):
    """The profile pane shows a social security and card number. Masking is
    applied by the browser as the image is produced, so no unredacted copy is
    ever written."""
    policy = Policy.load(ATTENDED)
    surface = WebSurface(page, gate=PolicyGate(policy),
                         mask_rules=policy.redaction.screenshot_mask)

    page.goto(f"{live_app}/login")
    page.fill("input[name=f1]", "roper")
    page.fill("input[name=f2]", "meridian-demo-pw")
    page.click("input[type=submit]")
    page.goto(f"{live_app}/members/100482")
    surface._settle()

    assert surface.mask_locators(), "the ssn and card fields matched no rule"

    # The values really are on the page - that is what makes this worth doing.
    ssn = surface.frame("detail").get_by_role("textbox", name="SSN")
    assert ssn.input_value() == "412-88-7390"

    surface.screenshot(tmp_path / "masked.png")
    surface.screenshot(tmp_path / "unmasked.png", mask=[])

    masked_bytes = (tmp_path / "masked.png").read_bytes()
    unmasked_bytes = (tmp_path / "unmasked.png").read_bytes()
    assert masked_bytes != unmasked_bytes, \
        "the masked capture is identical to the unmasked one, so nothing was covered"


def test_mask_patterns_survive_being_handed_to_the_browser():
    """A masking rule is evaluated as a JavaScript regular expression, where
    Python's inline `(?i)` is a parse error rather than a flag. Left unhandled
    the rule matches nothing and redaction silently stops - which looks exactly
    like it working."""
    from src.surface.web_playwright import browser_regex

    assert browser_regex("(?i)ssn|card").pattern == "ssn|card"
    assert browser_regex("(?i)ssn").flags & re.IGNORECASE
    assert browser_regex("SSN").search("ssn"), "matching must not depend on case"

    for path in (DISCOVERY, ATTENDED, UNATTENDED):
        for rule in Policy.load(path).redaction.screenshot_mask:
            assert "(?" not in browser_regex(rule.name_matches).pattern


def test_the_password_field_is_masked_on_the_sign_on_screen(page, live_app, tmp_path):
    policy = Policy.load(ATTENDED)
    surface = WebSurface(page, gate=PolicyGate(policy),
                         mask_rules=policy.redaction.screenshot_mask)
    page.goto(f"{live_app}/login")
    surface._settle()
    assert surface.mask_locators()


# -------------------------------------------------------------------- policies

@pytest.mark.parametrize("path", [DISCOVERY, ATTENDED, UNATTENDED])
def test_every_policy_loads_and_denies_the_control_surface(path):
    policy = Policy.load(path)
    assert policy.name
    assert policy.description.strip()
    gate = PolicyGate(policy)
    assert gate.check(NavigateAction(url="http://127.0.0.1:5000/_control/fault"),
                      "http://127.0.0.1:5000/members").verdict == "block"
    assert policy.redaction.screenshot_mask, "no masking rules"
    for pattern in policy.redaction.patterns:
        re.compile(pattern)
