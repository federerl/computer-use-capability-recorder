"""The discovery loop: observe, decide, act, until the goal is reached.

The model decides what to do; it never decides how to find it. Each turn it is
shown an inventory of the live surface and replies with one action against a
ref. The surface derives the targeting and records it. That split is what makes
a run reproducible: the recording contains locators computed from what was
actually on screen, not from what the model believed about it.

Every run is bounded - steps, wall clock, and model spend - and a run that hits
a bound still writes its evidence. A bounded stop is a result worth having;
an unbounded loop against a live application is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.artifact.schema import ValueSource, WaitSpec
from src.discovery import prompts
from src.discovery.tools import ToolCallError, tool_definitions, validate_value_call
from src.evidence.log import RunLog
from src.surface.base import (
    ActionBlocked, ClickAction, ConfirmationRequired, FillAction, FrameScope,
    NavigateAction, Observation, PressAction, SelectAction, SurfaceError, Target,
    TargetNotFound,
)
from src.surface.snapshot import render


class ModelClient(Protocol):
    """Just enough of the SDK surface to be substitutable in tests."""

    @property
    def messages(self) -> Any: ...


@dataclass
class DiscoveryConfig:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_steps: int = 25
    max_seconds: int = 360
    max_output_tokens: int = 4_096
    screenshot_every_step: bool = True


@dataclass
class RecordedStep:
    id: str
    action: str
    target: Target | None = None
    value: ValueSource | None = None
    key: str | None = None
    url: str | None = None
    wait: WaitSpec = field(default_factory=WaitSpec)
    note: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def as_dict(self) -> dict:
        return {
            "model_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass
class DiscoveryResult:
    status: str          # completed | gave_up | stopped_max_steps | stopped_timeout | error
    steps: list[RecordedStep] = field(default_factory=list)
    finalize: dict | None = None
    output_targets: dict[str, Target] = field(default_factory=dict)
    detail: str = ""
    usage: Usage = field(default_factory=Usage)
    observations: list[Observation] = field(default_factory=list)
    run_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.finalize is not None


class DiscoveryRun:
    def __init__(self, surface, client: ModelClient, log: RunLog, *,
                 goal: str, entry: str, inputs: dict[str, Any],
                 secrets: dict[str, str] | None = None,
                 config: DiscoveryConfig | None = None) -> None:
        self.surface = surface
        self.client = client
        self.log = log
        self.goal = goal
        self.entry = entry
        self.inputs = inputs
        self.secret_refs = secrets or {}
        self.config = config or DiscoveryConfig()

        self._secret_values: dict[str, str] = {}
        self._output_targets: dict[str, Target] = {}
        self._steps: list[RecordedStep] = []
        self._observations: list[Observation] = []
        self._usage = Usage()
        self._messages: list[dict] = []

    # ------------------------------------------------------------------ run

    def run(self) -> DiscoveryResult:
        from src.artifact.binding import read_secret

        for name, ref in self.secret_refs.items():
            value = read_secret(ref)
            self._secret_values[name] = value
            self.log.redactor.register_secret(name, value)

        tools = tool_definitions(list(self.inputs), list(self.secret_refs))
        if tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        system = [{"type": "text", "text": prompts.SYSTEM,
                   "cache_control": {"type": "ephemeral"}}]

        self.log.event("run_started", goal=self.goal, entry=self.entry,
                       inputs=self.inputs, secrets=list(self.secret_refs),
                       model=self.config.model, max_steps=self.config.max_steps)

        self.surface.act(NavigateAction(url=self.entry))
        self._steps.append(RecordedStep(id="s1", action="navigate", url=self.entry))

        self._messages = [{
            "role": "user",
            "content": prompts.goal_message(self.goal, self.inputs,
                                            list(self.secret_refs), self.entry),
        }]

        deadline = time.monotonic() + self.config.max_seconds
        pending_note = ""

        for index in range(1, self.config.max_steps + 1):
            if time.monotonic() > deadline:
                return self._finish("stopped_timeout",
                                    f"exceeded {self.config.max_seconds}s")

            observation = self._observe(index, pending_note)
            pending_note = ""

            try:
                response = self._ask(system, tools)
            except Exception as exc:  # network, auth, rate limit
                self.log.event("model_error", error=f"{type(exc).__name__}: {exc}")
                return self._finish("error", f"{type(exc).__name__}: {exc}")

            self._usage.add(response.usage)
            self._messages.append({"role": "assistant", "content": response.content})

            calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                text = self._text_of(response)
                self.log.event("model_said_nothing_actionable", text=text)
                pending_note = ("You did not call a tool. Call exactly one, or "
                                "call give_up if you are stuck.")
                self._messages.append({"role": "user", "content": pending_note})
                continue

            results, terminal = self._execute(calls, index, observation)
            self._messages.append({"role": "user", "content": results})

            if terminal:
                return self._finish(terminal[0], terminal[1], finalize=terminal[2])

        return self._finish("stopped_max_steps",
                            f"reached the {self.config.max_steps} step limit")

    # -------------------------------------------------------------- pieces

    def _observe(self, index: int, note: str) -> Observation:
        observation = self.surface.snapshot()
        self._observations.append(observation)
        inventory = render(observation)

        self.log.snapshot(index, inventory)
        if self.config.screenshot_every_step:
            try:
                self.surface.screenshot(self.log.screenshot_path(index))
            except Exception as exc:
                self.log.event("screenshot_failed", error=str(exc))

        self.log.event("observed", step=index, url=observation.url,
                       frames=observation.frames,
                       actionable=len(observation.actionable()))

        self._messages.append({
            "role": "user",
            "content": prompts.observation_message(index, observation.url,
                                                   inventory, note),
        })
        return observation

    def _ask(self, system: list[dict], tools: list[dict]):
        return self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_output_tokens,
            system=system,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            messages=self._messages,
        )

    def _execute(self, calls, index: int, observation: Observation):
        results: list[dict] = []
        terminal = None

        for call in calls:
            name, args = call.name, dict(call.input)
            self.log.event("tool_call", step=index, tool=name,
                           args=self._safe_args(name, args))

            if name == "finalize_capability":
                # Output targets are resolved now, while the final screen is
                # still on display. Afterwards it is gone.
                unresolved = self._capture_output_targets(args, observation)
                if unresolved:
                    results.append(self._result(
                        call,
                        "These outputs could not be located as labelled values: "
                        + "; ".join(unresolved)
                        + ". Point them at the node holding the value, in a row "
                          "that also carries its label.",
                        error=True))
                    continue
                results.append(self._result(call, "Recorded."))
                terminal = ("completed", "goal reached", args)
                continue
            if name == "give_up":
                results.append(self._result(call, "Stopped."))
                terminal = ("gave_up", args.get("reason", ""), None)
                continue

            try:
                detail = self._perform(name, args, observation)
                results.append(self._result(call, detail))
            except ToolCallError as exc:
                self.log.event("tool_rejected", step=index, tool=name, error=str(exc))
                results.append(self._result(call, str(exc), error=True))
            except TargetNotFound as exc:
                self.log.event("target_not_found", step=index, tool=name,
                               attempts=[a.describe() for a in exc.attempts])
                results.append(self._result(
                    call,
                    f"That ref could not be resolved uniquely: {exc}. Take a fresh "
                    f"look at the inventory.",
                    error=True))
            except (ActionBlocked, ConfirmationRequired) as exc:
                self.log.event("action_refused", step=index, tool=name, error=str(exc))
                results.append(self._result(call, str(exc), error=True))
            except SurfaceError as exc:
                self.log.event("surface_error", step=index, tool=name, error=str(exc))
                results.append(self._result(call, str(exc), error=True))

        return results, terminal

    def _perform(self, name: str, args: dict, observation: Observation) -> str:
        before = observation

        if name == "navigate":
            url = args["url"]
            self.surface.act(NavigateAction(url=url))
            self._record(RecordedStep(id=self._next_id(), action="navigate", url=url),
                         before)
            return f"Navigated to {url}."

        ref = args.get("ref", "")
        node = observation.by_ref(ref)
        if node is None:
            raise ToolCallError(
                f"{ref!r} is not in the inventory you were shown. Use a ref from "
                f"the most recent inventory."
            )
        target = self.surface.target_for(node, observation)

        if name == "click":
            self.surface.act(ClickAction(target=target))
            self._record(RecordedStep(id=self._next_id(), action="click", target=target),
                         before)
            return f"Clicked {node.role} {node.name!r}."

        if name == "press":
            key = args["key"]
            self.surface.act(PressAction(target=target, key=key))
            self._record(RecordedStep(id=self._next_id(), action="press",
                                      target=target, key=key), before)
            return f"Pressed {key}."

        if name in ("fill", "select"):
            validate_value_call(args, list(self.inputs), list(self.secret_refs))
            source, key = args["source"], args["name"]
            value, binding = self._value_for(source, key)

            action = (FillAction(target=target, value=value) if name == "fill"
                      else SelectAction(target=target, value=value))
            self.surface.act(action)
            self._record(RecordedStep(id=self._next_id(), action=name,
                                      target=target, value=binding), before)
            return f"Set {node.role} from {binding.describe()}."

        raise ToolCallError(f"Unknown tool {name!r}.")

    def _capture_output_targets(self, args: dict, observation: Observation) -> list[str]:
        """Turn the declared output refs into positional targets.

        Returns the ones that could not be located, so the model gets a chance
        to point somewhere usable rather than the run ending with outputs that
        would only work once.
        """
        problems: list[str] = []
        for spec in args.get("outputs", []):
            name, ref = spec.get("name", "?"), spec.get("ref", "")
            node = observation.by_ref(ref)
            if node is None:
                problems.append(f"{name} (ref {ref!r} is not in the inventory)")
                continue
            try:
                self._output_targets[name] = self.surface.extraction_target_for(
                    node, observation)
            except ValueError as exc:
                problems.append(f"{name} ({exc})")
        return problems

    def _value_for(self, source: str, key: str) -> tuple[str, ValueSource]:
        if source == "input":
            return str(self.inputs[key]), ValueSource(from_input=key)
        if source == "secret":
            return self._secret_values[key], ValueSource(from_secret=key)
        return key, ValueSource(literal=key)

    def _record(self, step: RecordedStep, before: Observation) -> None:
        step.wait = self._derive_wait(before)
        self._steps.append(step)
        self.log.event("step_recorded", step_id=step.id, action=step.action,
                       target=step.target, value=step.value, wait=step.wait)

    def _derive_wait(self, before: Observation) -> WaitSpec:
        """Infer what "done" looks like from what the action actually changed.

        A heading that was not on screen before is a much better completion
        signal than generic quiescence, and it is available for free here
        because we can see both states. Where nothing distinctive appears, fall
        back to settling.
        """
        try:
            after = self.surface.snapshot()
        except Exception:
            return WaitSpec()

        seen = {(n.frame, n.name) for n in before.nodes if n.role == "heading"}
        for node in after.nodes:
            if node.role == "heading" and node.name and (node.frame, node.name) not in seen:
                return WaitSpec(until="text", text=node.name,
                                scope=FrameScope(frame=node.frame))
        return WaitSpec()

    # -------------------------------------------------------------- helpers

    def _next_id(self) -> str:
        return f"s{len(self._steps) + 1}"

    def _safe_args(self, name: str, args: dict) -> dict:
        """A secret is referenced by name in the log, never by value. A literal
        still passes through the redactor on the way to disk."""
        if name in ("fill", "select") and args.get("source") == "secret":
            return {**args, "name": f"<<secret:{args.get('name')}>>"}
        return args

    @staticmethod
    def _result(call, text: str, error: bool = False) -> dict:
        return {"type": "tool_result", "tool_use_id": call.id,
                "content": text, "is_error": error}

    @staticmethod
    def _text_of(response) -> str:
        return " ".join(b.text for b in response.content
                        if getattr(b, "type", "") == "text")

    def _finish(self, status: str, detail: str, finalize: dict | None = None) -> DiscoveryResult:
        self.log.event("run_finished", status=status, detail=detail,
                       steps=len(self._steps), usage=self._usage.as_dict())
        return DiscoveryResult(
            status=status, steps=self._steps, finalize=finalize, detail=detail,
            output_targets=self._output_targets, usage=self._usage,
            observations=self._observations, run_id=self.log.run_id,
        )
