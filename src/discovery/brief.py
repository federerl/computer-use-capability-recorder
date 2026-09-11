"""What to discover, and with what.

The contract a capability will expose is declared before the run rather than
inferred from it. The model is told the names it must bind values to, so a
recording cannot end up parameterised by whatever the model happened to call
things - and two runs of the same brief produce capabilities with the same
interface.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.artifact.schema import AppProfile, ParamSpec, SecretRef


class DiscoveryBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    goal: str
    app_profile: AppProfile
    inputs: list[ParamSpec] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    values: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _values_cover_inputs(self) -> DiscoveryBrief:
        declared = {p.name for p in self.inputs}
        missing = {p.name for p in self.inputs if p.required} - set(self.values)
        if missing:
            raise ValueError(f"no value supplied for required inputs {sorted(missing)}")
        unknown = set(self.values) - declared
        if unknown:
            raise ValueError(f"values given for undeclared inputs {sorted(unknown)}")
        return self

    @property
    def secret_refs(self) -> dict[str, str]:
        return {s.name: s.ref for s in self.secrets}

    @classmethod
    def load(cls, path: str | Path) -> DiscoveryBrief:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
