"""The web implementation of Surface.

The only module in the project that knows a browser exists. Everything above it
works in roles, names and containment, which is why the same artifacts would
drive a desktop surface through a different implementation of this protocol.

Frames are first-class: a legacy servicing console usually puts the work in one,
and an accessibility snapshot of the top document cannot see inside it. Every
snapshot therefore walks all frames and labels each node with the frame it came
from.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml
from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, TimeoutError as PWTimeout

from . import locators as loc
from . import snapshot as snap
from .base import (
    ANY_FRAME, TOP_FRAME, Action, ActionBlocked, ActResult, ConfirmationRequired, Gate,
    AllowAll, Node, Observation, Resolution, SurfaceError, Target, TargetNotFound,
)

SETTLE_MS = 5_000
POLL_MS = 120


class FrameNotFound(SurfaceError):
    """A target names a frame that is not on the page. Distinct from a missing
    control: the pane the step expected is not there at all."""


class WebSurface:
    """A live browser page, observed through its accessibility tree."""

    def __init__(self, page: Page, gate: Gate | None = None) -> None:
        self.page = page
        self.gate = gate or AllowAll()

    # ------------------------------------------------------------------ frames

    def frames(self) -> dict[str, object]:
        """Stable frame names. The top document is always `main`.

        A child frame is named from its own element attributes rather than from
        the framework's cached `Frame.name`, which is not yet populated straight
        after a click-driven navigation. Frame names are recorded into artifacts,
        so a name that varies between observations of the same page would make
        every target in that frame unreplayable.
        """
        out: dict[str, object] = {}
        for i, fr in enumerate(self.page.frames):
            if i == 0:
                out[TOP_FRAME] = fr
                continue
            out[self._frame_name(fr, i)] = fr
        return out

    @staticmethod
    def _frame_name(fr, index: int) -> str:
        if fr.name:
            return fr.name
        try:
            el = fr.frame_element()
            for attr in ("name", "id", "title"):
                value = el.get_attribute(attr)
                if value:
                    return value
        except (PWError, PWTimeout):
            pass  # detached mid-navigation
        return f"frame{index}"

    def frame(self, name: str = TOP_FRAME):
        frames = self.frames()
        fr = frames.get(name)
        if fr is None:
            raise FrameNotFound(
                f"no frame named {name!r}; present: {sorted(frames)}"
            )
        return fr

    # -------------------------------------------------------------- observation

    def snapshot(self) -> Observation:
        nodes: list[Node] = []
        names: list[str] = []
        counter = 0

        for name, fr in self.frames().items():
            try:
                raw = fr.locator("body").aria_snapshot()
                tree = yaml.safe_load(raw)
            except (PWError, PWTimeout, yaml.YAMLError):
                # A frame can detach mid-navigation. Skip it rather than fail the
                # whole observation; the caller sees which frames were captured.
                continue

            frame_nodes = snap.parse_frame(tree, name, start_ref=counter)
            if frame_nodes:
                counter = max(
                    (int(n.ref[1:]) for n in frame_nodes if n.ref), default=counter
                )
            nodes.extend(frame_nodes)
            names.append(name)

        return Observation(nodes=nodes, frames=names, url=self.url())

    def url(self) -> str:
        return self.page.url

    def text(self, frame: str = TOP_FRAME) -> str:
        try:
            return self.frame(frame).locator("body").inner_text()
        except Exception:
            return ""

    # --------------------------------------------------------------- targeting

    def resolve(self, target: Target) -> Resolution:
        if target.scope.frame == ANY_FRAME:
            return self._resolve_across_frames(target)
        return loc.resolve(self.frame(target.scope.frame), target)

    def _resolve_across_frames(self, target: Target) -> Resolution:
        """Resolve a target that does not name a pane.

        Uniqueness still has to hold, and now it has to hold across the whole
        page: a match in two frames is as ambiguous as a match twice in one, so
        it fails rather than picking a frame.
        """
        hits: list[tuple[str, Resolution]] = []
        attempts = []

        for name in self.frames():
            try:
                hits.append((name, loc.resolve(self.frame(name), target)))
            except TargetNotFound as exc:
                attempts.extend(exc.attempts)

        if len(hits) == 1:
            resolution = hits[0][1]
            resolution.attempts = attempts + resolution.attempts
            return resolution
        if not hits:
            raise TargetNotFound(target, attempts)
        raise TargetNotFound(
            target,
            attempts + [a for _, r in hits for a in r.attempts],
        )

    def extraction_target_for(self, node: Node, obs: Observation) -> Target:
        """Ranked target for a value to be read back, positioned rather than named."""
        provisional = loc.extraction_target_for(node, obs)
        try:
            css = loc.css_path_for(self.resolve(provisional).locator)
        except (TargetNotFound, PWError):
            css = None
        return loc.extraction_target_for(node, obs, css=css)

    def target_for(self, node: Node, obs: Observation) -> Target:
        """Ranked strategies for a snapshot node, with a CSS path recorded from
        the live DOM as the last-resort fallback."""
        provisional = loc.strategies_for(node, obs)
        css = None
        try:
            css = loc.css_path_for(self.resolve(provisional).locator)
        except (TargetNotFound, PWError):
            pass
        return loc.strategies_for(node, obs, css=css)

    # ------------------------------------------------------------------- acting

    def act(self, action: Action) -> ActResult:
        """The single door to the browser.

        Every action is gated before it is performed, so policy cannot be
        bypassed by reaching for the page directly from somewhere else.
        """
        decision = self.gate.check(action, self.url())
        if decision.verdict == "block":
            raise ActionBlocked(action, decision)
        if decision.verdict == "confirm":
            raise ConfirmationRequired(action, decision)

        if action.action == "navigate":
            self.page.goto(action.url)
            self._settle()
            return ActResult(ok=True, action="navigate", url_after=self.url())

        resolution = self.resolve(action.target)
        locator = resolution.locator

        if action.action == "click":
            locator.click()
        elif action.action == "fill":
            locator.fill(action.value)
        elif action.action == "select":
            locator.select_option(action.value)
        elif action.action == "press":
            locator.press(action.key)
        else:  # pragma: no cover - Action is a closed union
            raise ValueError(f"unsupported action {action.action!r}")

        self._settle()
        return ActResult(
            ok=True, action=action.action, strategy_used=resolution.strategy,
            attempts=resolution.attempts, url_after=self.url(),
        )

    def _settle(self, timeout_ms: int = SETTLE_MS) -> None:
        """Wait for the frame tree to stop changing.

        Waiting on the page alone is not enough. In a framed application the
        click lands in a child frame, the top document never reloads, so a page
        level load-state wait returns immediately and the next observation reads
        the previous document. That race resolves differently on different runs,
        which is the exact shape of a replay that passes locally and fails
        elsewhere.

        Quiescence here is a floor, not a substitute for the per-step wait
        conditions an artifact declares. A slow page is a runtime condition for
        the caller to classify, not an exception to raise here.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        previous: tuple[str, ...] | None = None

        while time.monotonic() < deadline:
            for context in (self.page, *self.page.frames):
                try:
                    context.wait_for_load_state("domcontentloaded", timeout=POLL_MS)
                except (PWTimeout, PWError):
                    pass

            try:
                current = tuple(f.url for f in self.page.frames)
            except PWError:
                return  # page closed underneath us

            if current == previous:
                return
            previous = current

            try:
                self.page.wait_for_timeout(POLL_MS)
            except PWError:
                return

    # ----------------------------------------------------------------- evidence

    def screenshot(self, path: str | Path, mask: list | None = None) -> str:
        """Capture page state.

        `mask` is applied by the browser at capture time, so a redacted region is
        never written to disk in the first place.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(p), full_page=True, mask=mask or [])
        return str(p)
