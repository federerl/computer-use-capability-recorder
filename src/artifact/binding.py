"""Binding a capability's declared references to concrete values.

Two kinds of reference exist, and they are deliberately different.

A step's **value** is bound by name - `from_input`, `from_secret` - so a
credential or a task literal can never sit in the artifact at all.

A step's **target** may carry `${name}` placeholders. Some flows choose *which*
record to act on by what is on screen: the row for the Checking account, the
line for a given member. That choice is targeting, not typing, so it cannot be
expressed as a value binding, and hard-coding it would make the capability
single-use.

Substitution is deliberately dumb: exact placeholder, exact replacement, into an
accessibility name that is then matched exactly. There is no expression
language, and a placeholder naming something undeclared is rejected when the
artifact is validated rather than at replay time.
"""

from __future__ import annotations

import os
import re
from typing import Any

from src.surface.base import Target

TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Substitutable from the environment rather than from the caller's inputs.
RUNTIME_VARS = ("BASE_URL",)

# Strategy fields that name something on screen and may therefore vary per run.
TEMPLATABLE_FIELDS = ("name", "scope_name", "scope_anchor", "text", "value")


class UnboundReference(KeyError):
    """A placeholder with nothing to fill it. Never guessed at."""


def template_vars(value: Any) -> set[str]:
    """Every placeholder anywhere inside a string, model or container."""
    found: set[str] = set()

    if isinstance(value, str):
        return set(TEMPLATE.findall(value))
    if isinstance(value, dict):
        for v in value.values():
            found |= template_vars(v)
        return found
    if isinstance(value, (list, tuple)):
        for v in value:
            found |= template_vars(v)
        return found
    if hasattr(value, "model_dump"):
        return template_vars(value.model_dump())
    return found


def resolve(text: str, inputs: dict[str, Any], runtime: dict[str, str] | None = None) -> str:
    runtime = runtime or {}

    def swap(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in inputs and inputs[key] is not None:
            return str(inputs[key])
        if key in runtime:
            return runtime[key]
        if key in RUNTIME_VARS and os.environ.get(key):
            return os.environ[key]
        raise UnboundReference(
            f"{key!r} is referenced by the capability but was not supplied"
        )

    return TEMPLATE.sub(swap, text)


def bind_target(target: Target, inputs: dict[str, Any],
                runtime: dict[str, str] | None = None) -> Target:
    """A copy of the target with every placeholder filled.

    Returns a copy rather than mutating: the capability is loaded once and
    replayed many times, and a bound target must not leak into the next run.
    """
    data = target.model_dump()

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (resolve(v, inputs, runtime)
                    if isinstance(v, str) and k in TEMPLATABLE_FIELDS
                    else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return Target.model_validate(walk(data))


def read_secret(ref: str) -> str:
    """Resolve `env:NAME`. The value is never returned to anything that
    serialises, and never enters the artifact."""
    if not ref.startswith("env:"):
        raise ValueError(f"unsupported secret scheme in {ref!r}")
    name = ref[4:]
    value = os.environ.get(name)
    if value is None:
        raise UnboundReference(
            f"secret {ref!r} is not set in the environment"
        )
    return value
