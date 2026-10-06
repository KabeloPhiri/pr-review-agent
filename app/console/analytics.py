"""Console metrics, computed from the app's own MLflow traces.

Every review and apply already writes a trace. The pipeline tags each one
with the repository (`pr.repo` metadata) and its outcome (`prreview.*` tags),
and `llm_client.complete` records token usage per model call, which MLflow
totals into `TraceInfo.token_usage`. So this module needs no datastore of its
own: it reads trace summaries (no spans) and aggregates them.

The aggregation functions are pure and take a list of `TraceRecord`s, so the
tests drive them with hand-built records instead of a tracking server. The
MLflow query is the only part that talks to anything, behind `TraceSource`.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

REVIEW = "pr_review"
APPLY = "pr_apply"
APPLY_ALL = "pr_apply_all"

#: How many traces one console query reads at most. Above this the numbers
#: cover only the most recent traces, and the page says so.
MAX_TRACES = 5000


@dataclass(frozen=True)
class TraceRecord:
    """The summary of one trace the console needs — no spans."""

    trace_id: str
    kind: str
    timestamp_ms: int
    duration_ms: int
    state: str
    repo: str = ""
    pr: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """The operation raised (as opposed to a review that found problems)."""
        return self.state == "ERROR"

    def tag(self, name: str, default: str = "") -> str:
        return self.tags.get(f"prreview.{name}", default)

    def int_tag(self, name: str) -> int:
        try:
            return int(self.tag(name, "0") or 0)
        except ValueError:
            return 0

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )


#: `(start_ms, end_ms) -> records`. Injected so tests never need MLflow.
TraceSource = Callable[[int, int], list[TraceRecord]]


def record_from_info(info: Any) -> TraceRecord:
    """Build a `TraceRecord` from an MLflow `TraceInfo`."""
    tags = dict(getattr(info, "tags", None) or {})
    meta = dict(getattr(info, "trace_metadata", None) or {})
    usage = dict(getattr(info, "token_usage", None) or {})
    state = getattr(info, "state", "")
    return TraceRecord(
        trace_id=str(getattr(info, "trace_id", "") or getattr(info, "request_id", "")),
        kind=tags.get("mlflow.traceName", ""),
        timestamp_ms=int(getattr(info, "timestamp_ms", 0) or 0),
        duration_ms=int(getattr(info, "execution_duration", 0) or 0),
        state=getattr(state, "value", str(state)),
        repo=meta.get("pr.repo", ""),
        pr=meta.get("pr.number", ""),
        model=meta.get("config.model_endpoint", ""),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        tags={k: str(v) for k, v in tags.items() if k.startswith("prreview.")},
    )


def mlflow_trace_source(experiment_id: str | None = None) -> TraceSource:
    """Read trace summaries from the app's experiment (`MLFLOW_EXPERIMENT_ID`)."""

    def source(start_ms: int, end_ms: int) -> list[TraceRecord]:
        import mlflow

        experiment = experiment_id or os.getenv("MLFLOW_EXPERIMENT_ID")
        if not experiment:
            raise RuntimeError("MLFLOW_EXPERIMENT_ID is not set, so there are no traces to read")
        traces = mlflow.search_traces(
            experiment_ids=[experiment],
            filter_string=f"trace.timestamp_ms >= {start_ms} AND trace.timestamp_ms < {end_ms}",
            order_by=["timestamp_ms DESC"],
            max_results=MAX_TRACES,
            include_spans=False,
            return_type="list",
        )
        return [record_from_info(trace.info) for trace in traces]

    return source


class CachedSource:
    """Wraps a `TraceSource` so repeated page loads reuse one query for 60s."""

    def __init__(self, source: TraceSource, ttl_seconds: float = 60.0) -> None:
        self._source = source
        self._ttl = ttl_seconds
        self._cache: dict[int, tuple[float, list[TraceRecord]]] = {}

    def last_days(self, days: int) -> list[TraceRecord]:
        now = time.time()
        hit = self._cache.get(days)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        end_ms = int(now * 1000)
        records = self._source(end_ms - days * 86_400_000, end_ms)
        self._cache[days] = (now, records)
        return records


# -- aggregation --------------------------------------------------------------


@dataclass
class ModelUsage:
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = 0.0


@dataclass
class Overview:
    reviews: int = 0
    passed: int = 0
    gated: int = 0
    failed: int = 0
    findings: int = 0
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    total_tokens: int = 0
    cost_usd: float | None = 0.0
    models: list[ModelUsage] = field(default_factory=list)
    tokens_by_day: list[tuple[str, int]] = field(default_factory=list)
    applies: int = 0
    applied: int = 0
    apply_outcomes: list[tuple[str, int]] = field(default_factory=list)
    truncated: bool = False

    @property
    def pass_rate(self) -> float | None:
        completed = self.passed + self.gated
        return self.passed / completed if completed else None


@dataclass
class RepositoryRow:
    repo: str
    reviews: int = 0
    passed: int = 0
    gated: int = 0
    failed: int = 0
    findings: int = 0
    total_tokens: int = 0
    cost_usd: float | None = 0.0
    applied: int = 0
    last_review_ms: int = 0
    last_model: str = ""
    custom_settings: bool = False

    @property
    def pass_rate(self) -> float | None:
        completed = self.passed + self.gated
        return self.passed / completed if completed else None

    @property
    def findings_per_review(self) -> float | None:
        completed = self.passed + self.gated
        return self.findings / completed if completed else None


def filter_repo(records: list[TraceRecord], repo: str | None) -> list[TraceRecord]:
    return [r for r in records if r.repo == repo] if repo else list(records)


def repositories_in(records: list[TraceRecord]) -> list[str]:
    return sorted({r.repo for r in records if r.repo})


def cost(record_or_usage: Any, prices: Mapping[str, Mapping[str, float]]) -> float | None:
    """USD from per-1M-token prices; None when the model has no price set."""
    price = prices.get(record_or_usage.model)
    if not price:
        return None
    return (
        record_or_usage.input_tokens * float(price.get("input", 0))
        + record_or_usage.output_tokens * float(price.get("output", 0))
    ) / 1_000_000


def _add_cost(total: float | None, part: float | None) -> float | None:
    """A total is only known if every part is; otherwise report it as unknown."""
    return None if total is None or part is None else total + part


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def overview(
    records: list[TraceRecord],
    *,
    repo: str | None = None,
    prices: Mapping[str, Mapping[str, float]] | None = None,
) -> Overview:
    prices = prices or {}
    records = filter_repo(records, repo)
    out = Overview(truncated=len(records) >= MAX_TRACES)
    durations: list[float] = []
    models: dict[str, ModelUsage] = {}
    by_day: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()

    for r in records:
        if r.total_tokens:
            usage = models.setdefault(r.model or "unknown", ModelUsage(model=r.model or "unknown"))
            usage.calls += 1
            usage.input_tokens += r.input_tokens
            usage.output_tokens += r.output_tokens
            usage.total_tokens += r.total_tokens
            usage.cost_usd = _add_cost(usage.cost_usd, cost(r, prices))
            by_day[r.day] += r.total_tokens
            out.total_tokens += r.total_tokens

        if r.kind == REVIEW:
            out.reviews += 1
            if r.failed:
                out.failed += 1
                continue
            durations.append(r.duration_ms / 1000)
            out.findings += r.int_tag("findings")
            if r.tag("passed") == "True":
                out.passed += 1
            elif r.tag("passed") == "False":
                out.gated += 1
        elif r.kind in (APPLY, APPLY_ALL):
            out.applies += 1
            if r.failed:
                outcomes["failed"] += 1
            elif r.tag("applied") == "True":
                out.applied += 1
                outcomes["applied"] += 1
            else:
                outcomes[r.tag("reason") or "not applied"] += 1

    out.p50_seconds = percentile(durations, 50)
    out.p95_seconds = percentile(durations, 95)
    out.models = sorted(models.values(), key=lambda m: m.total_tokens, reverse=True)
    for usage in out.models:
        out.cost_usd = _add_cost(out.cost_usd, usage.cost_usd)
    out.tokens_by_day = sorted(by_day.items())
    out.apply_outcomes = outcomes.most_common()
    return out


def repositories(
    records: list[TraceRecord],
    *,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    custom: set[str] | None = None,
) -> list[RepositoryRow]:
    prices = prices or {}
    custom = custom or set()
    rows: dict[str, RepositoryRow] = {}
    for r in records:
        if not r.repo:
            continue
        row = rows.setdefault(r.repo, RepositoryRow(repo=r.repo))
        row.total_tokens += r.total_tokens
        if r.total_tokens:
            row.cost_usd = _add_cost(row.cost_usd, cost(r, prices))
        if r.kind == REVIEW:
            row.reviews += 1
            if r.timestamp_ms > row.last_review_ms:
                row.last_review_ms = r.timestamp_ms
                row.last_model = r.model
            if r.failed:
                row.failed += 1
                continue
            row.findings += r.int_tag("findings")
            if r.tag("passed") == "True":
                row.passed += 1
            elif r.tag("passed") == "False":
                row.gated += 1
        elif r.kind in (APPLY, APPLY_ALL) and r.tag("applied") == "True":
            row.applied += max(1, r.int_tag("applied_count"))
    for name in custom:
        rows.setdefault(name, RepositoryRow(repo=name)).custom_settings = True
    return sorted(rows.values(), key=lambda row: row.last_review_ms, reverse=True)


def recent_reviews(
    records: list[TraceRecord], *, repo: str | None = None, limit: int = 100
) -> list[TraceRecord]:
    reviews = [r for r in filter_repo(records, repo) if r.kind == REVIEW]
    return sorted(reviews, key=lambda r: r.timestamp_ms, reverse=True)[:limit]


def sort_rows(rows: list[Any], key: str, descending: bool, allowed: set[str]) -> list[Any]:
    """Server-side table sort; unknown keys leave the order alone. None sorts last."""
    if key not in allowed:
        return rows
    present = [row for row in rows if getattr(row, key) is not None]
    missing = [row for row in rows if getattr(row, key) is None]
    return sorted(present, key=lambda row: getattr(row, key), reverse=descending) + missing
