"""Keeping regulated data and credentials out of everything we write down.

Redaction happens at the point of writing, not as a pass over finished files.
Anything that reaches a log, a trace or an artifact has already been through
here, so there is no window in which an unredacted copy exists on disk.

Two mechanisms, because they fail differently. Registered secrets are matched by
exact value: a credential we were handed and therefore know precisely. Patterns
catch data we were never told about - a social security number that happened to
be on a page we screenshotted.
"""

from __future__ import annotations

import re
from typing import Any

# Shapes that are regulated wherever they appear. Deliberately narrow: a rule
# broad enough to catch every six-digit member number would redact the very
# identifiers that make a log useful.
DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b\d{3}-\d{2}-\d{4}\b", "ssn"),
    (r"\b(?:\d[ -]?){13,19}\b", "card"),
)

MIN_SECRET_LENGTH = 4


class Redactor:
    def __init__(self, patterns: tuple[tuple[str, str], ...] = DEFAULT_PATTERNS) -> None:
        self._patterns = [(re.compile(p), label) for p, label in patterns]
        self._secrets: dict[str, str] = {}

    def register_secret(self, name: str, value: str) -> None:
        """Teach the redactor a credential so it can be masked by exact value.

        Short values are refused rather than registered: masking every
        occurrence of a two-character string would destroy the log it is
        supposed to protect.
        """
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets[value] = f"<<secret:{name}>>"

    def text(self, value: str) -> str:
        for secret, placeholder in self._secrets.items():
            value = value.replace(secret, placeholder)
        for pattern, label in self._patterns:
            value = pattern.sub(f"<<redacted:{label}>>", value)
        return value

    def scrub(self, value: Any) -> Any:
        """Redact recursively, preserving structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub(v) for v in value)
        if hasattr(value, "model_dump"):
            return self.scrub(value.model_dump(mode="json"))
        return value

    def holds_secret(self, value: str) -> bool:
        return any(secret in value for secret in self._secrets)
