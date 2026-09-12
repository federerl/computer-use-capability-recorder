"""Evaluating detectors and wait conditions against a live surface.

Six detectors, no more. Every one answers a question about the current state
with a yes or a no, computed from a snapshot. Nothing here interprets, infers,
or decides whether a page "looks right" - that is precisely the judgement replay
must not exercise, because judgement is what makes a run non-reproducible.

Anything the declared detectors do not match is unknown, and unknown is never
treated as success.
"""

from __future__ import annotations

import re
import time
from typing import Any

from src.artifact.schema import Condition, Detector, WaitSpec
from src.surface.base import ANY_FRAME, FrameScope, RoleNameStrategy, Target, TargetNotFound

POLL_MS = 150


def frame_text(surface, scope: FrameScope) -> str:
    if scope.frame == ANY_FRAME:
        return "\n".join(surface.text(name) for name in surface.frames())
    try:
        return surface.text(scope.frame)
    except Exception:
        return ""


def _count(surface, role: str, name: str | None, scope: FrameScope) -> int:
    target = Target(
        scope=scope,
        primary=RoleNameStrategy(role=role, name=name or ""),
    )
    frames = [scope.frame] if scope.frame != ANY_FRAME else list(surface.frames())
    total = 0
    for frame_name in frames:
        try:
            locator = surface.frame(frame_name).get_by_role(
                role, name=name, exact=True) if name else \
                surface.frame(frame_name).get_by_role(role)
            total += locator.count()
        except Exception:
            continue
    return total


def evaluate(detector: Detector, surface, extracted: dict[str, Any] | None = None) -> bool:
    """True when the detector matches the current state."""
    extracted = extracted or {}
    kind = detector.kind

    if kind == "text_present":
        return detector.value in frame_text(surface, detector.scope)

    if kind == "text_absent":
        return detector.value not in frame_text(surface, detector.scope)

    if kind == "url_matches":
        return re.search(detector.pattern, surface.url()) is not None

    if kind == "role_visible":
        return _count(surface, detector.role, detector.name, detector.scope) >= 1

    if kind == "count_at_least":
        return _count(surface, detector.role, detector.name, detector.scope) >= detector.minimum

    if kind == "matches":
        value = extracted.get(detector.extract)
        if value is None:
            return False
        return re.search(detector.pattern, str(value)) is not None

    raise ValueError(f"unknown detector {kind!r}")


def describe(detector: Detector) -> str:
    kind = detector.kind
    if kind in ("text_present", "text_absent"):
        return f"{kind} {detector.value!r} in frame {detector.scope.frame}"
    if kind == "url_matches":
        return f"url matches {detector.pattern!r}"
    if kind in ("role_visible", "count_at_least"):
        minimum = getattr(detector, "minimum", 1)
        return (f"at least {minimum} {detector.role} "
                f"{detector.name!r} in frame {detector.scope.frame}")
    if kind == "matches":
        return f"{detector.extract} matches {detector.pattern!r}"
    return kind


def observed(detector: Detector, surface, extracted: dict[str, Any] | None = None) -> str:
    """What was actually there, for a failure message.

    A failure that says only "expected X" leaves the reader to go and look. The
    point of recording this is that they should not have to.
    """
    kind = detector.kind
    if kind in ("text_present", "text_absent"):
        text = " ".join(frame_text(surface, detector.scope).split())
        return f"frame {detector.scope.frame} reads: {text[:300]!r}"
    if kind == "url_matches":
        return f"url is {surface.url()!r}"
    if kind in ("role_visible", "count_at_least"):
        return (f"found {_count(surface, detector.role, detector.name, detector.scope)} "
                f"matching nodes")
    if kind == "matches":
        return f"{detector.extract} was {(extracted or {}).get(detector.extract)!r}"
    return "unknown"


def first_match(conditions: list[Condition], surface,
                extracted: dict[str, Any] | None = None) -> Condition | None:
    """The first declared condition matching the current state.

    Order is the artifact's order, which makes the result reviewable: a person
    reading the capability can see which condition wins when two could match.
    """
    for condition in conditions:
        try:
            if evaluate(condition.detect, surface, extracted):
                return condition
        except Exception:
            continue
    return None


# ------------------------------------------------------------------- waiting

def wait_for(surface, spec: WaitSpec) -> bool:
    """Block until the step's declared completion state holds.

    A per-step condition beats generic quiescence: settling only says the page
    stopped changing, which is equally true of a confirmation screen and of a
    validation error sitting where the confirmation should be.
    """
    if spec.until == "settled":
        surface._settle(spec.timeout_ms)
        return True

    deadline = time.monotonic() + spec.timeout_ms / 1000
    while time.monotonic() < deadline:
        if _wait_satisfied(surface, spec):
            return True
        try:
            surface.page.wait_for_timeout(POLL_MS)
        except Exception:
            return False
    return _wait_satisfied(surface, spec)


def _wait_satisfied(surface, spec: WaitSpec) -> bool:
    if spec.until == "text":
        return bool(spec.text) and spec.text in frame_text(surface, spec.scope)
    if spec.until == "url":
        return bool(spec.pattern) and re.search(spec.pattern, surface.url()) is not None
    if spec.until == "role":
        return _count(surface, spec.role or "", spec.name, spec.scope) >= 1
    return True


def describe_wait(spec: WaitSpec) -> str:
    if spec.until == "text":
        return f"text {spec.text!r} in frame {spec.scope.frame}"
    if spec.until == "url":
        return f"url matching {spec.pattern!r}"
    if spec.until == "role":
        return f"{spec.role} {spec.name!r} in frame {spec.scope.frame}"
    return "the page to settle"
