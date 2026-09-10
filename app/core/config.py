"""Layered configuration.

Precedence, lowest to highest:

1. `app/defaults/config.yaml` shipped with the app
2. app environment variables (`PRREVIEW_<FIELD>`), set in `databricks.yml`
3. `.prreview/config.yaml` on the pull request's source branch
4. per-request overrides in the POST body

Everything the reviewer does is driven by the resolved `EffectiveConfig`, so
`GET /config/effective` is enough to debug a misbehaving repo without
reading server logs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.errors import ConfigError
from app.core.models import GateLevel

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "defaults" / "config.yaml"
ENV_PREFIX = "PRREVIEW_"

# Fields that accept a comma-separated string when they arrive from the
# environment (YAML and JSON sources already give us real lists).
_LIST_FIELDS = {"analyzers", "standards", "exclude_paths"}


class EffectiveConfig(BaseModel):
    """The fully resolved settings for one review."""

    model_config = {"extra": "forbid"}

    # --- plugin selection ---
    scm: str = "azure_devops"
    quality: str = "noop"
    reviewer: str = "llm"
    analyzers: list[str] = Field(default_factory=lambda: ["python", "pyspark", "sql"])

    # --- review model ---
    model_endpoint: str = "databricks-gpt-5-2"
    temperature: float = 0.0
    max_chunk_chars: int = 12000

    # --- scope limits ---
    max_files: int = 60
    max_findings_per_file: int = 10
    max_diff_bytes: int = 400000
    exclude_paths: list[str] = Field(default_factory=list)

    # --- invocation ---
    #: In `auto` mode, how long the caller waits before the review is handed
    #: off to the background and a job id is returned instead. Kept under the
    #: ~120s Databricks Apps connection timeout.
    sync_wait_seconds: float = 90.0

    # --- standards ---
    standards: list[str] = Field(default_factory=list)
    config_path: str = ".prreview/config.yaml"

    # --- outcome ---
    severity_gate: GateLevel = GateLevel.ERROR
    post_comments: bool = True
    post_status: bool = True
    status_genre: str = "pr-review-agent"
    status_name: str = "ai-code-review"

    def fingerprint(self) -> str:
        """Short stable hash, attached to traces so a review is reproducible."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_defaults() -> dict[str, Any]:
    if not DEFAULTS_PATH.exists():
        return {}
    data = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{DEFAULTS_PATH} must contain a YAML mapping")
    return data


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read `PRREVIEW_*` variables that match a known field."""
    environ = os.environ if environ is None else environ
    fields = set(EffectiveConfig.model_fields)
    out: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        field = key[len(ENV_PREFIX) :].lower()
        if field not in fields:
            continue
        if field in _LIST_FIELDS:
            out[field] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            out[field] = value
    return out


def parse_repo_config(raw: str | None) -> dict[str, Any]:
    """Parse `.prreview/config.yaml` fetched from the source branch.

    A malformed repo config is a configuration error the PR author can fix,
    so it is reported rather than silently ignored.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError("Could not parse .prreview/config.yaml", detail=str(exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(".prreview/config.yaml must contain a YAML mapping")
    return data


def resolve(
    *,
    repo_config: dict[str, Any] | None = None,
    request_overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> EffectiveConfig:
    merged: dict[str, Any] = {}
    for layer in (
        load_defaults(),
        env_overrides(environ),
        repo_config or {},
        request_overrides or {},
    ):
        merged.update({k: v for k, v in layer.items() if v is not None})

    unknown = set(merged) - set(EffectiveConfig.model_fields)
    if unknown:
        raise ConfigError(
            "Unknown configuration key(s): " + ", ".join(sorted(unknown)),
            detail="Valid keys: " + ", ".join(sorted(EffectiveConfig.model_fields)),
        )
    try:
        return EffectiveConfig(**merged)
    except ValidationError as exc:
        raise ConfigError("Invalid configuration", detail=exc.json()) from exc
