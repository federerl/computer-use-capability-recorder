"""Surface behaviour against the live target application."""

from __future__ import annotations

import pytest

from src.surface.base import (
    ActionBlocked, ClickAction, Decision, Disambiguation, FillAction, FrameScope,
    NavigateAction, RoleNameStrategy, ScopedRoleStrategy, SelectAction, Target,
    TargetNotFound,
)
from src.surface.web_playwright import WebSurface


@pytest.fixture()
def surface(signed_in):
    return WebSurface(signed_in)


def node(obs, role, name=None, frame=None):
    for n in obs.nodes:
        if n.role == role and (name is None or n.name == name) \
                and (frame is None or n.frame == frame):
            return n
    raise AssertionError(f"no {role} {name!r} in frame {frame!r}")


# ------------------------------------------------------------------- frames

def test_snapshot_sees_inside_the_frame(surface, live_app):
    surface.page.goto(f"{live_app}/members/100482")
    surface._settle()
    obs = surface.snapshot()

    assert obs.frames == ["main", "detail"]
    assert any(n.frame == "detail" for n in obs.nodes), \
        "the detail pane is where the work happens; a snapshot that misses it is blind"
    assert node(obs, "heading", frame="detail").name == "BARNES, ROSALIND"


def test_frame_name_is_stable_after_a_click_navigation(surface, live_app):
    """Frame names end up in artifacts. If the same pane is `detail` after a
    goto but `frame1` after a click, every target recorded in it is unreplayable."""
    surface.act(NavigateAction(url=f"{live_app}/members?f3=100482"))
    obs = surface.snapshot()
    view = next(n for n in obs.nodes if n.role == "link" and n.name == "View")
    surface.act(ClickAction(target=surface.target_for(view, obs)))

    assert surface.snapshot().frames == ["main", "detail"]


def test_frame_scope_is_recorded_on_targets(surface, live_app):
    surface.page.goto(f"{live_app}/members/100482")
    surface._settle()
    obs = surface.snapshot()
    target = surface.target_for(node(obs, "link", "Accounts", frame="detail"), obs)
    assert target.scope.frame == "detail"


def test_observation_follows_a_navigation_inside_the_frame(surface, live_app):
    """The top document never reloads when a framed link is clicked. An
    observation taken straight afterwards must still see the new document, or
    every subsequent step reads stale state."""
    surface.act(NavigateAction(url=f"{live_app}/members/100482"))
    obs = surface.snapshot()
    accounts = node(obs, "link", "Accounts", frame="detail")

    surface.act(ClickAction(target=surface.target_for(accounts, obs)))

    after = surface.snapshot()
    assert any(n.role == "heading" and n.name.startswith("Accounts")
               for n in after.nodes if n.frame == "detail"), \
        "snapshot returned the pre-click document"


# --------------------------------------------------------------- disambiguation

def test_identical_links_get_a_scoped_primary(surface, live_app):
    """Two rows, one link text. role+name identifies neither, so it must not be
    recorded as the primary strategy."""
    surface.page.goto(f"{live_app}/members?f3=100482")
    surface._settle()
    obs = surface.snapshot()

    views = [n for n in obs.nodes if n.role == "link" and n.name == "View"]
    assert len(views) == 2

    target = surface.target_for(views[1], obs)
    assert target.primary.by == "scoped_role", \
        "an ambiguous role+name must be demoted, not recorded as primary"
    assert target.primary.scope_anchor == "Savings ****9930", \
        "the row should be anchored on the cell that actually distinguishes it"

    resolution = surface.resolve(target)
    assert [a for a in resolution.attempts if a.matched == 1]


def test_resolve_refuses_to_guess_between_matches(surface, live_app):
    """A strategy matching two nodes has identified nothing. Picking the first is
    how automation acts on the wrong record."""
    surface.page.goto(f"{live_app}/members?f3=100482")
    surface._settle()

    ambiguous = Target(
        scope=FrameScope(frame="main"),
        primary=RoleNameStrategy(role="link", name="View"),
    )
    with pytest.raises(TargetNotFound) as exc:
        surface.resolve(ambiguous)

    assert exc.value.attempts[0].matched == 2
    assert "2 match(es)" in str(exc.value)


def test_positional_resolution_requires_an_explicit_opt_in(surface, live_app):
    surface.page.goto(f"{live_app}/members?f3=100482")
    surface._settle()

    positional = Target(
        scope=FrameScope(frame="main"),
        primary=RoleNameStrategy(role="link", name="View"),
        disambiguation=Disambiguation(expect_unique=False, nth=1),
    )
    assert surface.resolve(positional).locator.count() == 1


# ------------------------------------------------------------ unnamed controls

def test_unnamed_control_is_addressable_through_its_row(surface, live_app):
    surface.page.goto(f"{live_app}/members/100482")
    surface._settle()
    surface.frame("detail").goto(f"{live_app}/stoppay/new?m=100482&a=****4821")
    surface._settle()

    obs = surface.snapshot()
    textbox = node(obs, "textbox", "", frame="detail")
    target = surface.target_for(textbox, obs)

    assert target.primary.by == "scoped_role"
    assert target.primary.scope_role == "row"
    assert target.primary.scope_anchor == "Check Number"
    assert surface.resolve(target).locator.count() == 1


def test_anchored_scope_survives_the_value_it_contains(surface, live_app):
    """A container's accessible name includes the values of the controls inside
    it. Anchoring on the label cell is what keeps a recorded target valid after
    the form has been used."""
    surface.page.goto(f"{live_app}/members/100482")
    surface._settle()
    surface.frame("detail").goto(f"{live_app}/stoppay/new?m=100482&a=****4821")
    surface._settle()

    obs = surface.snapshot()
    combobox = node(obs, "combobox", frame="detail")
    target = surface.target_for(combobox, obs)

    anchored = target.primary
    by_name = next(s for s in target.fallbacks
                   if s.by == "scoped_role" and s.scope_name)
    assert anchored.scope_anchor == "Reason"
    assert by_name.scope_name == "Reason -- select --"

    surface.act(SelectAction(target=target, value="lost_check"))

    # The row is now named "Reason Lost check".
    assert surface.resolve(Target(scope=FrameScope(frame="detail"),
                                  primary=anchored)).locator.count() == 1
    with pytest.raises(TargetNotFound):
        surface.resolve(Target(scope=FrameScope(frame="detail"), primary=by_name))


def test_control_with_no_name_and_no_scope_is_reported_not_guessed(surface, live_app):
    """Better to say a node is not addressable than to invent a locator for it."""
    from src.surface.base import Node, Observation
    from src.surface.locators import strategies_for

    orphan = Node(ref="e1", frame="main", role="textbox", name="", depth=0)
    with pytest.raises(ValueError, match="not addressable"):
        strategies_for(orphan, Observation(nodes=[orphan]))


def test_css_is_recorded_last_and_labelled(surface, live_app):
    """A recorded selector is a real fallback for controls whose accessible
    relationships shift, but it encodes document structure, so it ranks last."""
    surface.page.goto(f"{live_app}/members/100482")
    surface._settle()
    surface.frame("detail").goto(f"{live_app}/stoppay/new?m=100482&a=****4821")
    surface._settle()

    obs = surface.snapshot()
    target = surface.target_for(node(obs, "combobox", frame="detail"), obs)

    css = [s for s in target.ranked() if s.by == "css"]
    assert css, "no selector recorded"
    assert target.ranked()[-1].by == "css"
    assert "brittle" in css[0].note


# ------------------------------------------------------------------ full flow

def test_flow_to_confirmation_through_act(surface, live_app):
    """Every step goes through act(), driven only by snapshot-derived targets."""
    surface.act(NavigateAction(url=f"{live_app}/members"))

    obs = surface.snapshot()
    surface.act(FillAction(
        target=surface.target_for(node(obs, "textbox", "Member Number"), obs),
        value="100482"))
    surface.act(ClickAction(
        target=surface.target_for(node(obs, "button", "Search"), obs)))

    obs = surface.snapshot()
    checking = next(n for n in obs.nodes if n.role == "link" and n.name == "View"
                    and "Checking" in (n.nearest_scope() or ("", ""))[1])
    surface.act(ClickAction(target=surface.target_for(checking, obs)))

    obs = surface.snapshot()
    surface.act(ClickAction(
        target=surface.target_for(node(obs, "link", "Accounts", frame="detail"), obs)))

    obs = surface.snapshot()
    stop = next(n for n in obs.nodes if n.role == "link" and n.name == "Stop Pay"
                and "Checking" in (n.nearest_scope() or ("", ""))[1])
    surface.act(ClickAction(target=surface.target_for(stop, obs)))

    obs = surface.snapshot()
    surface.act(FillAction(
        target=surface.target_for(node(obs, "textbox", "", frame="detail"), obs),
        value="1043"))
    surface.act(SelectAction(
        target=surface.target_for(node(obs, "combobox", frame="detail"), obs),
        value="lost_check"))
    surface.act(ClickAction(
        target=surface.target_for(
            node(obs, "button", "Submit Stop Payment", frame="detail"), obs)))

    assert "Stop Payment Confirmed" in surface.text("detail")


# ---------------------------------------------------------------- policy gate

def test_every_action_passes_through_the_gate(surface, live_app):
    """There is one door to the browser. If the gate refuses, nothing happens."""

    class BlockClicks:
        def __init__(self):
            self.seen = []

        def check(self, action, url):
            self.seen.append(action.action)
            if action.action == "click":
                return Decision(verdict="block", reason="clicks not permitted")
            return Decision(verdict="allow")

    gate = BlockClicks()
    surface.gate = gate
    surface.act(NavigateAction(url=f"{live_app}/members?f3=100482"))
    before = surface.url()

    obs = surface.snapshot()
    views = [n for n in obs.nodes if n.role == "link" and n.name == "View"]
    with pytest.raises(ActionBlocked, match="clicks not permitted"):
        surface.act(ClickAction(target=surface.target_for(views[0], obs)))

    assert surface.url() == before, "a blocked action must not reach the page"
    assert gate.seen == ["navigate", "click"]
