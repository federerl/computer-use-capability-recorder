"""The guardrail model: what the automation may touch, and what it may do.

One gate, sitting at the surface boundary, shared unmodified by discovery and
replay. Neither can act outside policy, and there is no second path to the
browser to remember to guard.

The gate answers three separate questions, and keeping them separate matters:

**Where** - an allowlist of origins and paths. Checked on the way in for a
navigation, and again on what an action landed on, because a click can leave the
application without announcing it.

**What** - which action types are permitted at all.

**How dangerous** - whether an action is reversible. This is deliberately
enforced here rather than left to the recording. A discovery run watched the
stop-payment submit succeed and recorded it as safe; it had no way to know the
action places a fee-bearing hold. Pattern matching on a control's name is a
crude way to notice, and it is honest about being crude - but it noticed, and a
capability that forgets to mark a step is still stopped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlparse

from src.surface.base import ALLOW, Action, Decision


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Allowlist(Strict):
    origins: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(default_factory=list)

    def verdict(self, url: str) -> str | None:
        """None when permitted, otherwise why not."""
        if not url or url == "about:blank":
            return None

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if self.origins and origin not in self.origins:
            return f"{origin} is not an allowed origin"

        path = parsed.path or "/"
        for pattern in self.deny_paths:
            if re.search(pattern, path):
                return f"{path} matches a denied path ({pattern})"

        if self.paths and not any(re.search(p, path) for p in self.paths):
            return f"{path} is not an allowed path"
        return None


class ActionTypes(Strict):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    def verdict(self, action: str) -> str | None:
        if action in self.deny:
            return f"{action} is denied"
        if self.allow and action not in self.allow:
            return f"{action} is not an allowed action type"
        return None


class RiskRule(Strict):
    """Names an action as consequential, and says what to do about it."""
    actions: list[str] = Field(default_factory=lambda: ["click"])
    name_matches: str
    risk: Literal["irreversible", "sensitive"] = "irreversible"
    on_risky: Literal["block", "require_confirmation", "flag"] = "require_confirmation"
    why: str = ""

    def matches(self, action_type: str, label: str) -> bool:
        if action_type not in self.actions:
            return False
        return bool(label) and re.search(self.name_matches, label) is not None


class MaskRule(Strict):
    """A region painted over before a screenshot is written.

    Masking happens at capture, in the browser, so the unredacted image never
    exists. Scrubbing a file afterwards is not redaction; it is deletion after
    disclosure.
    """
    role: str
    name_matches: str


class RedactionPolicy(Strict):
    patterns: list[str] = Field(default_factory=list)
    screenshot_mask: list[MaskRule] = Field(default_factory=list)


class Policy(Strict):
    name: str = "default"
    description: str = ""
    confirmations: Literal["require", "permit"] = "require"
    """What a step the capability marks as needing confirmation means here.

    `require` asks a person. `permit` is for a capability reviewed and approved
    to run alone - the profile grants that once, rather than every invocation
    passing a flag that quietly overrides the recording.
    """
    allowlist: Allowlist = Field(default_factory=Allowlist)
    action_types: ActionTypes = Field(default_factory=ActionTypes)
    risk_rules: list[RiskRule] = Field(default_factory=list)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        return cls.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def redaction_patterns(self) -> tuple[tuple[str, str], ...]:
        return tuple((p, "policy") for p in self.patterns_or_default())

    def patterns_or_default(self) -> list[str]:
        return self.redaction.patterns


def label_of(action: Action) -> str:
    """The human-visible name of whatever an action is aimed at.

    Risk rules read this, so they see what an operator would see, not a
    selector.
    """
    target = getattr(action, "target", None)
    if target is None:
        return ""
    for strategy in target.ranked():
        for field in ("name", "text"):
            value = getattr(strategy, field, None)
            if value:
                return str(value)
    return ""


class PolicyGate:
    """The single door between any caller and the browser."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.flagged: list[dict] = []

    def check(self, action: Action, url: str) -> Decision:
        reason = self.policy.action_types.verdict(action.action)
        if reason:
            return Decision(verdict="block", reason=reason)

        # A navigation is judged by where it is going; everything else by where
        # it already is.
        subject = action.url if action.action == "navigate" else url
        reason = self.policy.allowlist.verdict(subject)
        if reason:
            return Decision(verdict="block", reason=reason)

        label = label_of(action)
        for rule in self.policy.risk_rules:
            if not rule.matches(action.action, label):
                continue
            detail = f"{label!r} looks {rule.risk}" + (f": {rule.why}" if rule.why else "")
            if rule.on_risky == "block":
                return Decision(verdict="block", reason=detail)
            if rule.on_risky == "require_confirmation":
                return Decision(verdict="confirm", reason=detail)
            self.flagged.append({"label": label, "risk": rule.risk, "why": rule.why})
            return Decision(verdict="flag", reason=detail)

        return ALLOW

    def check_landing(self, url: str) -> Decision:
        """Where an action actually took us.

        Checked separately because a click cannot be judged in advance: the
        allowlist protects the application boundary, and a link is perfectly
        capable of leaving it.
        """
        reason = self.policy.allowlist.verdict(url)
        if reason:
            return Decision(verdict="block", reason=f"landed outside policy: {reason}")
        return ALLOW

    def risk_of(self, action: Action) -> str:
        label = label_of(action)
        for rule in self.policy.risk_rules:
            if rule.matches(action.action, label):
                return rule.risk
        return "safe"
