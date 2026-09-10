"""Turn a per-frame ARIA snapshot into an addressable inventory.

The snapshot is the model's only view of the surface. It is deliberately not the
DOM: it carries roles, accessible names and containment, which is what targeting
is expressed in, and none of the markup detail that makes legacy pages enormous
and unstable.

Containment is the part that earns its place. A control with no accessible name
is not addressable on its own, but "the textbox inside the row named Check
Number" is - and that relationship is structural, not geometric.
"""

from __future__ import annotations

import re

from .base import ACTIONABLE_ROLES, Node, Observation

# Roles that get a ref the model can act on or read from.
REF_ROLES = ACTIONABLE_ROLES | {"cell"}

# Pure layout wrappers. Dropping them keeps the inventory small without losing
# any relationship the targeting strategies rely on.
SKIP_ROLES = {"rowgroup", "generic", "none", "presentation", "list", "img"}

# `role "accessible name" [attr=value]` - name and attributes both optional.
_KEY = re.compile(
    r'^(?P<role>[A-Za-z][\w-]*)'
    r'(?:\s+"(?P<name>.*)")?'
    r'(?P<attrs>(?:\s*\[[^\]]*\])*)\s*$'
)


def _parse_key(key: str) -> tuple[str, str]:
    """Split a snapshot key into (role, accessible name)."""
    m = _KEY.match(key.strip())
    if not m:
        return key.strip(), ""
    name = m.group("name") or ""
    # Playwright escapes quotes inside names; unescape so the value matches what
    # get_by_role will compare against.
    return m.group("role"), name.replace('\\"', '"')


class _Builder:
    def __init__(self, frame: str, start: int) -> None:
        self.frame = frame
        self.counter = start
        self.nodes: list[Node] = []

    def _ref(self, role: str) -> str:
        if role not in REF_ROLES:
            return ""
        self.counter += 1
        return f"e{self.counter}"

    def emit(self, role: str, name: str, depth: int,
             ancestors: tuple[tuple[str, str], ...], text: str = "") -> Node:
        node = Node(
            ref=self._ref(role), frame=self.frame, role=role, name=name,
            depth=depth, ancestors=ancestors, text=text,
        )
        self.nodes.append(node)
        return node

    def walk(self, items, depth: int, ancestors: tuple[tuple[str, str], ...]) -> None:
        if items is None:
            return
        if not isinstance(items, list):
            items = [items]

        for item in items:
            if isinstance(item, str):
                role, name = _parse_key(item)
                if role in SKIP_ROLES:
                    continue
                self.emit(role, name, depth, ancestors)
                continue

            if not isinstance(item, dict):
                continue

            for key, value in item.items():
                if not isinstance(key, str) or key.startswith("/"):
                    continue  # snapshot metadata, e.g. /url

                role, name = _parse_key(key)

                if role == "text":
                    # Free text between elements. Kept so state assertions can
                    # see it; never a target.
                    self.emit("text", "", depth, ancestors,
                              text=str(value) if value is not None else "")
                    continue

                if role in SKIP_ROLES:
                    self.walk(value, depth, ancestors)
                    continue

                text = value if isinstance(value, str) else ""
                node = self.emit(role, name, depth, ancestors, text=text)

                if isinstance(value, list):
                    self.walk(value, depth + 1, (*ancestors, (node.role, node.name)))


def parse_frame(snapshot: object, frame: str, start_ref: int = 0) -> list[Node]:
    """Parse one frame's already-deserialised ARIA snapshot."""
    b = _Builder(frame, start_ref)
    b.walk(snapshot, 0, ())
    return b.nodes


def render(obs: Observation, max_name: int = 80) -> str:
    """Compact inventory for the model.

    Refs appear only on nodes that can be acted on or read from. Structure is
    kept because it is what disambiguates identically named controls, but it is
    kept cheaply - one line each, no attributes, no markup.
    """
    out: list[str] = []
    current = None

    for n in obs.nodes:
        if n.frame != current:
            current = n.frame
            out.append(f"frame:{n.frame}")

        if n.role == "text":
            body = " ".join(n.text.split())
            if not body:
                continue
            out.append(f"{'  ' * (n.depth + 1)}text {body[:max_name]!r}")
            continue

        pad = "  " * (n.depth + 1)
        label = f" {n.name[:max_name]!r}" if n.name else ""
        body = f" = {' '.join(n.text.split())[:max_name]!r}" if n.text else ""
        ref = f"  [{n.ref}]" if n.ref else ""
        out.append(f"{pad}{n.role}{label}{body}{ref}")

    return "\n".join(out)


def summarise(obs: Observation) -> str:
    """One line describing what the snapshot contains. For logs."""
    return (
        f"{len(obs.nodes)} nodes, {len(obs.actionable())} actionable, "
        f"frames={obs.frames}"
    )
