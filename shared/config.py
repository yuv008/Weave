"""Typed loader for ``config/weave.yaml``. Path comes from ``WEAVE_CONFIG``."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.acl import ORG_WIDE, SYSTEMS, parse_acl

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
    org_wide: bool = False


class LLMConfig(BaseModel):
    """The agents' model. ``groq`` calls Groq; ``none`` runs the rule-based router, planner and composer.

    ``rpm`` caps Groq calls in this process. A full bucket waits for a free slot.
    ``shared_daily_cap`` limits /answer and /wisp/mail per company per day on the
    server key; a company with its own key, and the home company, are not counted.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = "none"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    rpm: int = Field(default=30, gt=0)
    shared_daily_cap: int | None = Field(default=200, gt=0)


class AnswerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=6, gt=0, le=12)
    citation_policy: Literal["drop", "flag"] = "drop"
    recall_limit: int = Field(default=5, ge=0)
    note_max_chars: int = Field(default=200, gt=0)
    cache_ttl_seconds: float = Field(default=60, ge=0)
    llm: LLMConfig = Field(default_factory=LLMConfig)


class WatcherConfig(BaseModel):
    """The impact watcher worker (``python -m agents.watcher``)."""

    model_config = ConfigDict(extra="forbid")

    batch: int = Field(default=20, gt=0)
    every: int = Field(default=60, gt=0)
    send: Literal["dry_run", "slack"] = "dry_run"


class WeaveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    embedding: EmbeddingConfig
    likely_threshold: float = Field(ge=0, le=1)
    hide_provisional: bool
    acl_stale_minutes: int = Field(gt=0)
    hidden_sources_notice: bool = False
    figure_stale_days: int = Field(default=30, gt=0)
    expertise_half_life_days: int = Field(default=30, gt=0)
    authority: dict[str, int]
    sources: dict[str, SourcePolicy]
    answer: AnswerConfig = Field(default_factory=AnswerConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)

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


def source_acl(group: str, policy: SourcePolicy) -> list[str]:
    """``acl_groups`` for a source and its items: its own group, plus ``org:all`` when org-wide."""

    return [group, ORG_WIDE] if policy.org_wide else [group]


def load_config(path: str | Path | None = None) -> WeaveConfig:
    import yaml

    resolved = Path(path or os.environ.get("WEAVE_CONFIG") or DEFAULT_PATH)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return WeaveConfig.model_validate(data)
