"""Typed loader for ``config/weave.yaml``. Path comes from ``WEAVE_CONFIG``."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.acl import SYSTEMS, parse_acl

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "weave.yaml"


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    dim: int = Field(gt=0)


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    min_level: int = Field(ge=0, le=3)
    cascade_policy: Literal["broadcast", "manual"]
    authority: int = Field(ge=1, le=3)


class WeaveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    embedding: EmbeddingConfig
    likely_threshold: float = Field(ge=0, le=1)
    hide_provisional: bool
    acl_stale_minutes: int = Field(gt=0)
    authority: dict[str, int]
    sources: dict[str, SourcePolicy]

    @field_validator("authority")
    @classmethod
    def _known_systems(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - set(SYSTEMS)
        if unknown:
            raise ValueError(f"unknown systems in authority: {sorted(unknown)}")
        return value

    @field_validator("sources")
    @classmethod
    def _acl_keys(cls, value: dict[str, SourcePolicy]) -> dict[str, SourcePolicy]:
        for key in value:
            parse_acl(key)
        return value


def load_config(path: str | Path | None = None) -> WeaveConfig:
    import yaml

    resolved = Path(path or os.environ.get("WEAVE_CONFIG") or DEFAULT_PATH)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return WeaveConfig.model_validate(data)
