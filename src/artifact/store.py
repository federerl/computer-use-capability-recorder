"""Reading, writing and versioning capability artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import SCHEMA_VERSION, Capability

SCHEMA_PATH = Path("schema/capability.schema.json")


class SchemaVersionError(Exception):
    """The artifact was written against an incompatible schema.

    Refusing outright is deliberate. A capability from a newer major version may
    contain steps, detectors or conditions this engine does not understand, and
    executing the parts it recognises would be worse than not running at all.
    """


def _major(version: str) -> int:
    return int(version.split(".", 1)[0])


def loads(raw: str) -> Capability:
    data = json.loads(raw)
    found = data.get("schema_version", "0.0.0")
    if _major(found) > _major(SCHEMA_VERSION):
        raise SchemaVersionError(
            f"artifact schema {found} is newer than supported {SCHEMA_VERSION}; "
            f"refusing to execute a flow this engine may only partly understand"
        )
    return Capability.model_validate(data)


def load(path: str | Path) -> Capability:
    return loads(Path(path).read_text(encoding="utf-8"))


def dumps(capability: Capability) -> str:
    return json.dumps(
        capability.model_dump(mode="json", by_alias=True, exclude_none=True),
        indent=2,
    ) + "\n"


def save(capability: Capability, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(capability), encoding="utf-8")
    return p


def json_schema() -> dict:
    """The artifact contract, generated from the models that validate it, so the
    published schema cannot drift from what is actually enforced."""
    schema = Capability.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Capability"
    schema["description"] = (
        "A typed, versioned description of a UI flow that can be replayed "
        "deterministically without a model in the decision loop."
    )
    return schema


def export_json_schema(path: str | Path = SCHEMA_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    return p
