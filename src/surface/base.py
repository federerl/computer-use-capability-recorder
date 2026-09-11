"""Surface contracts: how we perceive a surface, and how we act on it.

This module is the seam. Everything above it - the recorded flow, the replay
engine, the artifact - is written against these types and knows nothing about
Playwright, a browser, or a DOM. A desktop surface driven through UI Automation
would implement the same protocol and reuse the same artifacts.

Targeting is expressed in accessibility vocabulary because that vocabulary
survives serialisation and carries across surfaces. Coordinates do neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol, Union, runtime_checkable

from pydantic import BaseModel, Field

# Roles a user can operate. Everything else is structure: useful for scoping and
# for asserting state, never a click target.
ACTIONABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "switch", "slider", "spinbutton",
})

# Roles that make a meaningful scope for a nested control. A row named
# "Check Number" is how an unnamed input inside it becomes addressable.
SCOPING_ROLES = ("row", "dialog", "form", "listitem", "article", "region", "group", "table")

TOP_FRAME = "main"

# Some states are not tied to a pane. A session-expiry banner or an unexpected
# interstitial appears wherever the application decided to put it, so a target
# for one has to be able to say "wherever this is" without becoming a wildcard
# that matches several things at once.
ANY_FRAME = "any"


# --------------------------------------------------------------------- targeting

class RoleNameStrategy(BaseModel):
    """Role plus accessible name. First choice: it is what a screen reader would
    use, and it is stable across restyling."""
    by: Literal["role_name"] = "role_name"
    role: str
    name: str


class ScopedRoleStrategy(BaseModel):
    """A control identified by the container it sits in.

    Covers the two cases role+name cannot: a control with no accessible name at
    all, and several identically named controls that differ only by which row
    they sit in.

    The container is identified one of two ways. `scope_anchor` names a distinct
    cell the container must contain - a label like "Check Number", or the account
    a row is about. `scope_name` matches the container's own accessible name,
    which is the concatenation of everything inside it.

    Anchoring is preferred because a container's accessible name includes the
    values of the controls within it. A row named "Reason -- select --" becomes
    "Reason Lost check" the moment the field is used, so a strategy written
    against the whole name stops matching the row it was recorded from.
    """
    by: Literal["scoped_role"] = "scoped_role"
    scope_role: str
    scope_name: str | None = None
    scope_anchor: str | None = None
    role: str
    name: str | None = None


class TextExactStrategy(BaseModel):
    by: Literal["text_exact"] = "text_exact"
    text: str


class CssStrategy(BaseModel):
    """Recorded from the live DOM. Ranked last and labelled, because it encodes
    document structure that can change without any visible change to the UI."""
    by: Literal["css"] = "css"
    value: str
    note: str = "recorded selector; brittle"


Strategy = Annotated[
    Union[RoleNameStrategy, ScopedRoleStrategy, TextExactStrategy, CssStrategy],
    Field(discriminator="by"),
]


class FrameScope(BaseModel):
    frame: str = TOP_FRAME


class Disambiguation(BaseModel):
    """`expect_unique` is the safety property. A strategy that matches more than
    one node has not identified anything, so it is discarded in favour of the
    next strategy rather than resolved by position. `nth` exists only for targets
    that are genuinely positional, and must be set deliberately."""
    expect_unique: bool = True
    nth: int | None = None


class Target(BaseModel):
    scope: FrameScope = Field(default_factory=FrameScope)
    primary: Strategy
    fallbacks: list[Strategy] = Field(default_factory=list)
    disambiguation: Disambiguation = Field(default_factory=Disambiguation)

    def ranked(self) -> list[Strategy]:
        return [self.primary, *self.fallbacks]


# ----------------------------------------------------------------------- actions

class ClickAction(BaseModel):
    action: Literal["click"] = "click"
    target: Target


class FillAction(BaseModel):
    """`value` is already resolved. The binding that produced it - an input
    parameter or a secret reference - lives in the artifact step, never here, so
    a secret cannot reach the surface layer as a literal by accident."""
    action: Literal["fill"] = "fill"
    target: Target
    value: str


class SelectAction(BaseModel):
    action: Literal["select"] = "select"
    target: Target
    value: str


class PressAction(BaseModel):
    action: Literal["press"] = "press"
    target: Target
    key: str


class NavigateAction(BaseModel):
    action: Literal["navigate"] = "navigate"
    url: str


Action = Annotated[
    Union[ClickAction, FillAction, SelectAction, PressAction, NavigateAction],
    Field(discriminator="action"),
]


# ------------------------------------------------------------------ observation

@dataclass(frozen=True)
class Node:
    """One node of an accessibility snapshot."""
    ref: str
    frame: str
    role: str
    name: str
    depth: int
    ancestors: tuple[tuple[str, str], ...] = ()   # (role, name) from outermost in
    text: str = ""

    @property
    def actionable(self) -> bool:
        return self.role in ACTIONABLE_ROLES

    def nearest_scope(self) -> tuple[str, str] | None:
        """Innermost named ancestor usable as a scope."""
        for role, name in reversed(self.ancestors):
            if role in SCOPING_ROLES and name:
                return role, name
        return None


@dataclass
class Observation:
    """A full-page snapshot: every frame, in document order."""
    nodes: list[Node] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    url: str = ""

    def by_ref(self, ref: str) -> Node | None:
        return next((n for n in self.nodes if n.ref == ref), None)

    def actionable(self) -> list[Node]:
        return [n for n in self.nodes if n.actionable]


# ----------------------------------------------------------------- policy gate

class Decision(BaseModel):
    verdict: Literal["allow", "block", "confirm", "flag"]
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict in ("allow", "flag")


ALLOW = Decision(verdict="allow")


@runtime_checkable
class Gate(Protocol):
    """The single choke point between any caller and the surface.

    Every action passes through a gate before it reaches the browser. Discovery
    and replay share one implementation, so neither can act outside policy, and
    there is no second code path to guard.
    """

    def check(self, action: Action, url: str) -> Decision: ...


class AllowAll:
    """Placeholder gate. Replaced by the policy engine; the choke point exists
    from the start so nothing has to be restructured to introduce it."""

    def check(self, action: Action, url: str) -> Decision:  # noqa: ARG002
        return ALLOW


# ---------------------------------------------------------------------- results

@dataclass
class Attempt:
    """One strategy tried, and what it matched. The full list is what makes a
    targeting failure debuggable instead of merely reported."""
    strategy: Strategy
    matched: int
    error: str = ""

    def describe(self) -> str:
        s = self.strategy.model_dump(exclude_defaults=True)
        return f"{self.strategy.by}({s}) -> {self.matched} match(es)" + (
            f" [{self.error}]" if self.error else ""
        )


@dataclass
class Resolution:
    locator: object                 # framework handle; opaque above this layer
    strategy: Strategy
    attempts: list[Attempt]


@dataclass
class ActResult:
    ok: bool
    action: str
    strategy_used: Strategy | None = None
    attempts: list[Attempt] = field(default_factory=list)
    url_after: str = ""
    detail: str = ""


# ----------------------------------------------------------------------- errors

class SurfaceError(Exception):
    """Base for every failure originating at the surface boundary."""


class TargetNotFound(SurfaceError):
    def __init__(self, target: Target, attempts: list[Attempt]):
        self.target = target
        self.attempts = attempts
        tried = "; ".join(a.describe() for a in attempts) or "no strategies"
        super().__init__(
            f"no unique match in frame {target.scope.frame!r}; tried: {tried}"
        )


class ActionBlocked(SurfaceError):
    def __init__(self, action: Action, decision: Decision):
        self.action = action
        self.decision = decision
        super().__init__(f"{action.action} blocked by policy: {decision.reason}")


class ConfirmationRequired(SurfaceError):
    """Raised for an action the policy will not perform unattended. The caller
    routes this to a human rather than deciding for itself."""

    def __init__(self, action: Action, decision: Decision):
        self.action = action
        self.decision = decision
        super().__init__(f"{action.action} requires confirmation: {decision.reason}")


# --------------------------------------------------------------------- protocol

@runtime_checkable
class Surface(Protocol):
    """What a surface must provide. A web page today; a desktop window later."""

    def snapshot(self) -> Observation: ...

    def resolve(self, target: Target) -> Resolution: ...

    def act(self, action: Action) -> ActResult: ...

    def url(self) -> str: ...

    def text(self, frame: str = TOP_FRAME) -> str: ...
