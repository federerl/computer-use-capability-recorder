"""The tools the model is given, and how their calls are validated.

The shape of these schemas is a design decision, not plumbing. A step's value
must be declared as coming from an input or a secret *at the moment it is
typed*, so parameterisation is a property of the recording rather than something
reconstructed afterwards by guessing which strings look like arguments. A model
that types a member number as a literal has produced a capability that only ever
works for that member, and we would rather catch that during the run than
discover it on the first replay.

The model never writes a locator. It acts on a ref from the inventory it was
shown, and the surface derives the targeting.
"""

from __future__ import annotations

from typing import Any

ACT_TOOLS = ("click", "fill", "select", "press", "navigate")


def _string(desc: str, enum: list[str] | None = None) -> dict:
    spec: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        spec["enum"] = enum
    return spec


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _value_properties(input_names: list[str], secret_names: list[str]) -> dict:
    return {
        "ref": _string("Ref of the control from the current inventory, e.g. e12."),
        "source": _string(
            "Where this value comes from. Use 'input' for anything the task "
            "supplied, 'secret' for credentials, and 'literal' only for a value "
            "that is a fixed part of the flow itself and would be identical on "
            "every invocation.",
            enum=["input", "secret", "literal"],
        ),
        "name": _string(
            "When source is 'input', one of: " + (", ".join(input_names) or "(none)") +
            ". When source is 'secret', one of: " + (", ".join(secret_names) or "(none)") +
            ". When source is 'literal', the text to type."
        ),
    }


def tool_definitions(input_names: list[str], secret_names: list[str]) -> list[dict]:
    return [
        _tool(
            "click",
            "Click a control.",
            {"ref": _string("Ref of the control from the current inventory.")},
            ["ref"],
        ),
        _tool(
            "fill",
            "Type into a text field, replacing whatever is there.",
            _value_properties(input_names, secret_names),
            ["ref", "source", "name"],
        ),
        _tool(
            "select",
            "Choose an option in a dropdown. Give the option's underlying value.",
            _value_properties(input_names, secret_names),
            ["ref", "source", "name"],
        ),
        _tool(
            "press",
            "Press a key while a control is focused, e.g. Enter.",
            {
                "ref": _string("Ref of the control from the current inventory."),
                "key": _string("Key name, e.g. Enter or Tab."),
            },
            ["ref", "key"],
        ),
        _tool(
            "navigate",
            "Go directly to a URL within the application. Prefer operating the "
            "interface; use this only to start, or to recover from a dead end.",
            {"url": _string("Absolute URL.")},
            ["url"],
        ),
        _tool(
            "finalize_capability",
            "Call this once the goal is reached and the resulting state is on "
            "screen. Describes what the flow returns and how to tell it worked.",
            {
                "title": _string("Short name for the capability."),
                "description": _string("One or two sentences on what it does."),
                "success_text": _string(
                    "Text visible on the final screen that would not be present "
                    "if the flow had not succeeded. Avoid text that also appears "
                    "on the form itself."
                ),
                "success_frame": _string("Frame the success text appears in."),
                "outputs": {
                    "type": "array",
                    "description": "Values the caller should get back.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": _string("snake_case name for the value."),
                            "ref": _string(
                                "Ref of the node holding the value in the current "
                                "inventory."
                            ),
                            "type": _string("JSON type.",
                                            enum=["string", "number", "integer", "boolean"]),
                            "pattern": {
                                "type": ["string", "null"],
                                "description": "Regex the value should match, or null.",
                            },
                            "description": _string("What this value is."),
                        },
                        "required": ["name", "ref", "type", "pattern", "description"],
                        "additionalProperties": False,
                    },
                },
                "anticipated_conditions": {
                    "type": "array",
                    "description": (
                        "Legitimate non-success results a caller would need to be "
                        "told about, such as a record not existing. These are "
                        "answers, not errors. Only describe ones you can identify "
                        "from text this application would show."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": _string("SCREAMING_SNAKE code."),
                            "text": _string("Text that identifies this state."),
                            "frame": _string("Frame the text appears in."),
                            "outcome": _string("snake_case name for the caller."),
                            "message": _string("Plain sentence for a human."),
                        },
                        "required": ["code", "text", "frame", "outcome", "message"],
                        "additionalProperties": False,
                    },
                },
            },
            ["title", "description", "success_text", "success_frame",
             "outputs", "anticipated_conditions"],
        ),
        _tool(
            "give_up",
            "Stop. Use this when the goal cannot be reached, or when proceeding "
            "would mean acting on something you are not certain about.",
            {"reason": _string("What blocked you, specifically.")},
            ["reason"],
        ),
    ]


class ToolCallError(Exception):
    """A tool call that cannot be honoured as written.

    Surfaced back to the model as a tool result rather than ending the run: a
    mistyped ref or an unbound value is something it can correct, and a run that
    stops on the first malformed call wastes the steps already spent.
    """


def validate_value_call(args: dict, input_names: list[str], secret_names: list[str]) -> None:
    source, name = args.get("source"), args.get("name", "")

    if source == "input" and name not in input_names:
        raise ToolCallError(
            f"{name!r} is not a declared input. Declared inputs: "
            f"{', '.join(input_names) or '(none)'}."
        )
    if source == "secret" and name not in secret_names:
        raise ToolCallError(
            f"{name!r} is not a declared secret. Declared secrets: "
            f"{', '.join(secret_names) or '(none)'}."
        )
    if source == "literal" and not name:
        raise ToolCallError("A literal value cannot be empty.")
