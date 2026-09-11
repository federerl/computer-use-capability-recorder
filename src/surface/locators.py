"""Turn snapshot nodes into ranked targeting strategies, and resolve them.

Two rules govern everything here.

**The model never writes a selector.** It acts on a snapshot ref; this module
derives the strategies. That is what keeps a recording reproducible - the
targeting in an artifact is computed from what was on screen, not from what a
model believed about it.

**Ambiguity is a failure, not a tie to break.** A strategy that matches several
nodes has identified nothing. It is discarded in favour of the next strategy,
and if none is unique the resolution fails with every attempt recorded. Choosing
the first match would let a replay do the right thing to the wrong record.
"""

from __future__ import annotations

from .base import (
    SCOPING_ROLES,
    Attempt, CssStrategy, Disambiguation, FrameScope, Node, Observation,
    Resolution, RoleNameStrategy, ScopedRoleStrategy, Strategy, Target,
    TargetNotFound, TextExactStrategy,
)

# Roles whose visible text is their accessible name, so text is a real fallback.
_TEXT_ADDRESSABLE = {"link", "button", "menuitem", "tab", "option"}

# A CSS path is recorded for a control when the DOM is in front of us. It is
# ranked last: it encodes document structure, which can change with no visible
# change to the page.
_CSS_PATH_JS = """
el => {
  const seg = n => {
    if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1)
      return '#' + CSS.escape(n.id);
    let s = n.nodeName.toLowerCase();
    if (n.name) s += `[name="${n.name}"]`;
    const sibs = Array.from(n.parentNode ? n.parentNode.children : [])
                      .filter(c => c.nodeName === n.nodeName);
    if (sibs.length > 1) s += `:nth-of-type(${sibs.indexOf(n) + 1})`;
    return s;
  };
  const parts = [];
  for (let n = el; n && n.nodeType === 1 && parts.length < 6; n = n.parentElement) {
    parts.unshift(seg(n));
    if (n.id) break;
  }
  return parts.join(' > ');
}
"""


def _same_name_count(obs: Observation, node: Node) -> int:
    return sum(
        1 for n in obs.nodes
        if n.frame == node.frame and n.role == node.role and n.name == node.name
    )


def _container_of(obs: Observation, node: Node) -> tuple[int, Node] | None:
    """The innermost scoping container holding `node`, by document position.

    Position rather than name, so two rows with identical accessible names are
    still told apart.
    """
    try:
        index = obs.nodes.index(node)
    except ValueError:
        return None
    for i in range(index - 1, -1, -1):
        candidate = obs.nodes[i]
        if candidate.frame != node.frame:
            break
        if candidate.depth < node.depth and candidate.role in SCOPING_ROLES:
            return i, candidate
    return None


def _anchor_for(obs: Observation, node: Node) -> str | None:
    """A cell inside the node's container whose text identifies that container.

    Chosen so it is stable and distinct: it must not be the cell wrapping the
    control itself (that one holds the value), and its text must occur exactly
    once among the cells of the frame, or it identifies nothing.
    """
    found = _container_of(obs, node)
    if not found:
        return None
    start, container = found

    cell_names: dict[str, int] = {}
    for n in obs.nodes:
        if n.frame == node.frame and n.role == "cell" and n.name:
            cell_names[n.name] = cell_names.get(n.name, 0) + 1

    node_index = obs.nodes.index(node)
    for i in range(start + 1, len(obs.nodes)):
        cell = obs.nodes[i]
        if cell.depth <= container.depth or cell.frame != node.frame:
            break                      # left the container
        if cell.role != "cell" or not cell.name:
            continue
        if i < node_index and cell.depth < node.depth and _wraps(obs, i, node_index, cell):
            continue                   # the cell the control sits in
        if cell_names.get(cell.name) == 1:
            return cell.name
    return None


def _wraps(obs: Observation, cell_index: int, node_index: int, cell: Node) -> bool:
    """True when everything between the cell and the node stays inside the cell."""
    return all(
        obs.nodes[j].depth > cell.depth for j in range(cell_index + 1, node_index + 1)
    )


def strategies_for(node: Node, obs: Observation, css: str | None = None) -> Target:
    """Build the ranked strategy list for a node.

    Ranking is verified against the snapshot the node came from rather than
    assumed: if role+name already matches more than one node on the page, it is
    demoted below the scoped strategy instead of being recorded as the primary
    and failing on every replay.
    """
    named = bool(node.name)
    unique_by_name = named and _same_name_count(obs, node) == 1
    scope = node.nearest_scope()
    anchor = _anchor_for(obs, node)

    role_name = RoleNameStrategy(role=node.role, name=node.name) if named else None

    anchored = (
        ScopedRoleStrategy(
            scope_role=scope[0], scope_anchor=anchor,
            role=node.role, name=node.name or None,
        )
        if scope and anchor else None
    )
    scoped = (
        ScopedRoleStrategy(
            scope_role=scope[0], scope_name=scope[1],
            role=node.role, name=node.name or None,
        )
        if scope and scope[1] else None
    )
    text = TextExactStrategy(text=node.name) if named and node.role in _TEXT_ADDRESSABLE else None
    css_s = CssStrategy(value=css) if css else None

    # A container's own name embeds the values of the controls inside it, so the
    # anchored form outranks it wherever an anchor exists.
    ordered: list[Strategy | None] = (
        [role_name, anchored, scoped, text, css_s] if unique_by_name
        else [anchored, scoped, role_name, text, css_s]
    )
    ranked = [s for s in ordered if s is not None]

    if not ranked:
        raise ValueError(
            f"{node.role!r} node has no accessible name and no named ancestor to "
            f"scope it by; it is not addressable (ref={node.ref!r})"
        )

    return Target(
        scope=FrameScope(frame=node.frame),
        primary=ranked[0],
        fallbacks=ranked[1:],
        disambiguation=Disambiguation(expect_unique=True),
    )


def row_cell_index(obs: Observation, node: Node) -> int | None:
    """Position of `node` among the cells of its container, in document order."""
    found = _container_of(obs, node)
    if not found:
        return None
    start, container = found

    position = 0
    for i in range(start + 1, len(obs.nodes)):
        candidate = obs.nodes[i]
        if candidate.depth <= container.depth or candidate.frame != node.frame:
            break
        if candidate.role != "cell":
            continue
        if candidate is node:
            return position
        position += 1
    return None


def extraction_target_for(node: Node, obs: Observation,
                          css: str | None = None) -> Target:
    """Target a value by where it sits, never by what it says.

    An extracted value is the one thing on the page guaranteed to differ between
    runs. The cell holding a confirmation number is *named* by that number, so
    the ordinary ranking would record `cell "SP-89802443"` as the primary
    strategy and bake one run's answer into the artifact.

    Positional resolution is correct here rather than a compromise: the value is
    identified as the nth cell of the row carrying a stable label, which is
    exactly how a person reads a label/value table.
    """
    anchor = _anchor_for(obs, node)
    scope = node.nearest_scope()
    index = row_cell_index(obs, node)

    if not (anchor and scope and index is not None):
        raise ValueError(
            f"{node.role} {node.name!r} is not in a labelled container, so it "
            f"cannot be extracted without recording its current value"
        )

    fallbacks: list[Strategy] = [CssStrategy(value=css)] if css else []
    return Target(
        scope=FrameScope(frame=node.frame),
        primary=ScopedRoleStrategy(
            scope_role=scope[0], scope_anchor=anchor, role=node.role,
        ),
        fallbacks=fallbacks,
        disambiguation=Disambiguation(expect_unique=False, nth=index),
    )


def build_locator(frame, strategy: Strategy):
    """Map one strategy onto a framework locator."""
    if strategy.by == "role_name":
        return frame.get_by_role(strategy.role, name=strategy.name, exact=True)

    if strategy.by == "scoped_role":
        if strategy.scope_anchor:
            scope = frame.get_by_role(strategy.scope_role).filter(
                has=frame.get_by_role("cell", name=strategy.scope_anchor, exact=True)
            )
        elif strategy.scope_name is not None:
            scope = frame.get_by_role(strategy.scope_role, name=strategy.scope_name,
                                      exact=True)
        else:
            raise ValueError("scoped_role needs either scope_anchor or scope_name")

        if strategy.name:
            return scope.get_by_role(strategy.role, name=strategy.name, exact=True)
        return scope.get_by_role(strategy.role)

    if strategy.by == "text_exact":
        return frame.get_by_text(strategy.text, exact=True)

    if strategy.by == "css":
        return frame.locator(strategy.value)

    raise ValueError(f"unknown strategy {strategy.by!r}")


def resolve(frame, target: Target) -> Resolution:
    """Try each strategy in order; the first unique match wins.

    Every strategy tried is recorded, matched count included, so a failure says
    what was looked for and what was actually on the page.
    """
    attempts: list[Attempt] = []
    want_nth = target.disambiguation.nth

    for strategy in target.ranked():
        try:
            locator = build_locator(frame, strategy)
            count = locator.count()
        except Exception as exc:  # unknown role, malformed selector, detached frame
            attempts.append(Attempt(strategy=strategy, matched=-1,
                                    error=f"{type(exc).__name__}: {exc}"))
            continue

        attempts.append(Attempt(strategy=strategy, matched=count))

        if count == 1:
            return Resolution(locator=locator, strategy=strategy, attempts=attempts)

        if count > 1 and want_nth is not None and want_nth < count:
            # Positional resolution only when the target declares it deliberately.
            return Resolution(locator=locator.nth(want_nth), strategy=strategy,
                              attempts=attempts)

    raise TargetNotFound(target, attempts)


def css_path_for(locator) -> str | None:
    """Record a CSS path for a resolved control. Best effort: absence of a CSS
    fallback is not a failure, since it is the least trustworthy strategy."""
    try:
        return locator.evaluate(_CSS_PATH_JS)
    except Exception:
        return None
