"""A scripted stand-in for the model.

The discovery loop is mostly plumbing - inventory in, tool call out, action
performed, recording written - and none of that needs a model to be exercised.
Driving it from a script means the loop is debugged before a single paid run,
and the parts that must hold (value binding, target derivation, refusal
handling) are pinned by tests that run anywhere, with no key.

The script selects controls the way the model must: by role, accessible name,
and what contains them. It cannot reach a control the inventory does not show.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

_LINE = re.compile(
    r"^(?P<indent> *)(?P<role>[a-z]+)(?: '(?P<name>.*?)')?(?: = '.*?')?"
    r"(?:  \[(?P<ref>e\d+)\])?$"
)


@dataclass
class InventoryLine:
    frame: str
    role: str
    name: str
    ref: str
    raw: str


def parse_inventory(text: str) -> list[InventoryLine]:
    lines: list[InventoryLine] = []
    frame = "main"
    for raw in text.splitlines():
        if raw.startswith("frame:"):
            frame = raw.split(":", 1)[1].strip()
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        lines.append(InventoryLine(
            frame=frame, role=m.group("role"), name=m.group("name") or "",
            ref=m.group("ref") or "", raw=raw,
        ))
    return lines


def find_ref(inventory: str, role: str, name: str | None = None, *,
             under: str | None = None, index: int = 0,
             frame: str | None = None) -> str:
    """Locate a ref the way the script intends it, or say why it could not."""
    lines = parse_inventory(inventory)

    start = 0
    if under is not None:
        start = next(
            (i + 1 for i, line in enumerate(lines) if under in line.raw),
            -1,
        )
        if start == 0 or start == -1:
            raise AssertionError(f"no line containing {under!r}\n{inventory}")

    matches = [
        line for line in lines[start:]
        if line.role == role
        and (name is None or line.name == name)
        and (frame is None or line.frame == frame)
        and line.ref
    ]
    if len(matches) <= index:
        raise AssertionError(
            f"no {role} {name!r} (under={under!r}, index={index}) in:\n{inventory}"
        )
    return matches[index].ref


@dataclass
class Select:
    role: str
    name: str | None = None
    under: str | None = None
    index: int = 0
    frame: str | None = None

    def ref(self, inventory: str) -> str:
        return find_ref(inventory, self.role, self.name, under=self.under,
                        index=self.index, frame=self.frame)


@dataclass
class Move:
    """One scripted turn: a tool, and how to find what it acts on."""
    tool: str
    select: Select | None = None
    args: dict = field(default_factory=dict)


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 1200
        self.output_tokens = 90
        self.cache_read_input_tokens = 800
        self.cache_creation_input_tokens = 0


class _Response:
    def __init__(self, blocks: list[_Block]) -> None:
        self.content = blocks
        self.usage = _Usage()
        self.stop_reason = "tool_use"


class _Messages:
    def __init__(self, outer: "FakeModel") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> _Response:
        return self._outer._next(kwargs)


class FakeModel:
    """Replays a script of moves, reading refs out of the inventory it is given."""

    def __init__(self, moves: list[Move]) -> None:
        self.moves = list(moves)
        self.calls: list[dict] = []
        self.messages = _Messages(self)
        self._ids = itertools.count(1)
        self._cursor = 0

    def _inventory(self, kwargs: dict) -> str:
        for message in reversed(kwargs["messages"]):
            content = message.get("content")
            if message["role"] == "user" and isinstance(content, str) \
                    and "Inventory:" in content:
                return content.split("Inventory:", 1)[1]
        raise AssertionError("no inventory was sent to the model")

    def _next(self, kwargs: dict) -> _Response:
        self.calls.append(kwargs)
        if self._cursor >= len(self.moves):
            raise AssertionError("script exhausted; the loop asked for another move")

        move = self.moves[self._cursor]
        self._cursor += 1

        args = dict(move.args)
        if move.select is not None:
            args["ref"] = move.select.ref(self._inventory(kwargs))

        return _Response([
            _Block(type="text", text=f"Calling {move.tool}."),
            _Block(type="tool_use", id=f"call_{next(self._ids)}",
                   name=move.tool, input=args),
        ])

    # ------------------------------------------------------------ assertions

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self.moves)

    def sent_tools(self) -> list[str]:
        return [t["name"] for t in self.calls[0]["tools"]]
