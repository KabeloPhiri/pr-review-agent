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

import json
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
    def outcome_recorded(self) -> bool:
        """Outcome tags exist only on traces written since the console shipped."""
        return any(key in self.tags for key in ("prreview.passed", "prreview.applied"))

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
    repo, pr = meta.get("pr.repo", ""), meta.get("pr.number", "")
    if not repo and meta.get("pr.slug") and meta.get("pr.scm"):
        # Traces written before repository tagging still carry the PR slug
        # (`owner/repo#9`), so older reviews can be grouped too.
        slug, _, number = meta["pr.slug"].rpartition("#")
        repo, pr = f"{meta['pr.scm']}/{slug.replace('/', '__')}", pr or number
    return TraceRecord(
        trace_id=str(getattr(info, "trace_id", "") or getattr(info, "request_id", "")),
        kind=tags.get("mlflow.traceName", ""),
        timestamp_ms=int(getattr(info, "timestamp_ms", 0) or 0),
        duration_ms=int(getattr(info, "execution_duration", 0) or 0),
        state=getattr(state, "value", str(state)),
        repo=repo,
        pr=pr,
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
    #: Reviews from before outcome tagging: counted, but not in the pass rate.
    unrecorded_reviews: int = 0
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
            else:
                out.unrecorded_reviews += 1
        elif r.kind in (APPLY, APPLY_ALL):
            out.applies += 1
            if r.failed:
                outcomes["failed"] += 1
            elif not r.outcome_recorded:
                outcomes["outcome not recorded"] += 1
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


# -- quality: false positives, accepted fixes, duplicates ------------------------

FEEDBACK = "pr_feedback"


@dataclass
class QualityRow:
    """One rule, rule family, model or repository."""

    key: str
    posted: int = 0
    flagged: int = 0
    accepted: int = 0

    @property
    def fp_rate(self) -> float | None:
        return self.flagged / self.posted if self.posted else None


@dataclass
class Flag:
    timestamp_ms: int
    repo: str
    pr: str
    rule: str
    file: str
    model: str
    reason: str
    by: str


@dataclass
class Duplicate:
    timestamp_ms: int
    repo: str
    pr: str
    file: str
    line: str
    old_rule: str
    new_rule: str


@dataclass
class Quality:
    posted: int = 0
    flagged: int = 0
    accepted: int = 0
    by_rule: list[QualityRow] = field(default_factory=list)
    by_family: list[QualityRow] = field(default_factory=list)
    by_model: list[QualityRow] = field(default_factory=list)
    by_repo: list[QualityRow] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)

    @property
    def fp_rate(self) -> float | None:
        return self.flagged / self.posted if self.posted else None


def rule_family(rule: str) -> str:
    """`python.typing.missing` -> `python`: roughly the standard it came from."""
    return rule.split(".", 1)[0] if rule else "unknown"


def _json_tag(record: TraceRecord, name: str) -> Any:
    try:
        return json.loads(record.tag(name) or "null")
    except ValueError:
        return None


def quality(records: list[TraceRecord], *, repo: str | None = None) -> Quality:
    """False-positive rates: flags (`/fp`) over findings posted, per dimension."""
    records = filter_repo(records, repo)
    out = Quality()
    tables: dict[str, dict[str, QualityRow]] = {
        "rule": {}, "family": {}, "model": {}, "repo": {}
    }

    def bump(dimension: str, key: str, attr: str, amount: int = 1) -> None:
        row = tables[dimension].setdefault(key, QualityRow(key=key))
        setattr(row, attr, getattr(row, attr) + amount)

    def count(rule: str, model: str, repo_name: str, attr: str, amount: int = 1) -> None:
        bump("rule", rule, attr, amount)
        bump("family", rule_family(rule), attr, amount)
        bump("model", model or "unknown", attr, amount)
        if repo_name:
            bump("repo", repo_name, attr, amount)

    for r in records:
        if r.kind == REVIEW and not r.failed:
            rules = _json_tag(r, "rules")
            if isinstance(rules, dict):
                for rule, n in rules.items():
                    count(rule, r.model, r.repo, "posted", int(n))
                    out.posted += int(n)
            for d in _json_tag(r, "duplicate_rules") or []:
                out.duplicates.append(
                    Duplicate(r.timestamp_ms, r.repo, r.pr, d.get("file", ""), d.get("line", ""),
                              d.get("old", ""), d.get("new", ""))
                )
        elif r.kind == FEEDBACK and r.tag("feedback") == "false_positive":
            rule = r.tag("rule") or "unknown"
            model = r.tag("finding_model") or r.model
            count(rule, model, r.repo, "flagged")
            out.flagged += 1
            out.flags.append(
                Flag(r.timestamp_ms, r.repo, r.pr, rule, r.tag("file"), model,
                     r.tag("fp_reason"), r.tag("flagged_by"))
            )
        elif r.kind in (APPLY, APPLY_ALL) and r.tag("applied") == "True":
            applied = _json_tag(r, "rules") if r.kind == APPLY_ALL else {r.tag("rule"): 1}
            for rule, n in (applied or {}).items():
                if rule:
                    count(rule, r.model, r.repo, "accepted", int(n))
                    out.accepted += int(n)

    def ranked(dimension: str) -> list[QualityRow]:
        return sorted(
            tables[dimension].values(),
            key=lambda row: (row.flagged, row.fp_rate or 0, row.posted),
            reverse=True,
        )

    out.by_rule, out.by_family = ranked("rule"), ranked("family")
    out.by_model, out.by_repo = ranked("model"), ranked("repo")
    out.flags.sort(key=lambda f: f.timestamp_ms, reverse=True)
    out.duplicates.sort(key=lambda d: d.timestamp_ms, reverse=True)
    return out
