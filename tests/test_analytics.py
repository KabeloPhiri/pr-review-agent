"""Console metrics over hand-built trace records spanning two repositories."""

from types import SimpleNamespace

import pytest

from app.console import analytics
from app.console.analytics import TraceRecord

A = "github/acme__payments"
B = "github/acme__ledger"
DAY = 86_400_000
T0 = 1_791_000_000_000  # a fixed instant, so `day` is stable


def review(repo, *, passed=True, findings=0, model="m1", tokens=(100, 10), ms=2000, t=T0,
           failed=False):
    return TraceRecord(
        trace_id=f"tr-{repo}-{t}-{findings}",
        kind=analytics.REVIEW,
        timestamp_ms=t,
        duration_ms=ms,
        state="ERROR" if failed else "OK",
        repo=repo,
        pr="7",
        model=model,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        total_tokens=sum(tokens),
        tags={} if failed else {"prreview.passed": str(passed), "prreview.findings": str(findings)},
    )


def apply(repo, *, applied=True, reason=None, count=1, kind=analytics.APPLY, t=T0):
    tags = {"prreview.applied": str(applied), "prreview.reason": reason or ""}
    if kind == analytics.APPLY_ALL:
        tags["prreview.applied_count"] = str(count)
    return TraceRecord(
        trace_id=f"ap-{repo}-{t}-{reason}",
        kind=kind,
        timestamp_ms=t,
        duration_ms=500,
        state="OK",
        repo=repo,
        model="m1",
        tags=tags,
    )


RECORDS = [
    review(A, passed=True, findings=1, ms=1000),
    review(A, passed=False, findings=4, ms=3000, t=T0 + DAY),
    review(A, failed=True, t=T0 + DAY),
    review(B, passed=True, findings=0, model="m2", tokens=(1000, 200), ms=8000),
    apply(A),
    apply(A, applied=False, reason="no_change_generated"),
    apply(B, kind=analytics.APPLY_ALL, count=3),
]


def test_overview_counts_reviews_outcomes_and_failures():
    data = analytics.overview(RECORDS)
    assert (data.reviews, data.passed, data.gated, data.failed) == (4, 2, 1, 1)
    assert data.pass_rate == pytest.approx(2 / 3)
    assert data.findings == 5
    assert data.p50_seconds == 3.0 and data.p95_seconds == 8.0  # failed run excluded


def test_overview_tokens_per_model_and_day():
    data = analytics.overview(RECORDS)
    usage = {m.model: m for m in data.models}
    assert usage["m1"].total_tokens == 330  # three m1 reviews, 110 each
    assert usage["m2"].total_tokens == 1200
    assert data.total_tokens == 1530
    assert [day for day, _ in data.tokens_by_day] == sorted(day for day, _ in data.tokens_by_day)


def test_overview_apply_outcomes():
    data = analytics.overview(RECORDS)
    assert (data.applies, data.applied) == (3, 4)  # the /apply all applied three
    assert dict(data.apply_outcomes) == {"applied": 2, "no_change_generated": 1}


def test_repo_filter_returns_only_that_repository():
    data = analytics.overview(RECORDS, repo=B)
    assert (data.reviews, data.total_tokens, data.applies) == (1, 1200, 1)


def test_all_repositories_is_the_sum_of_each():
    whole = analytics.overview(RECORDS)
    parts = [analytics.overview(RECORDS, repo=r) for r in (A, B)]
    assert whole.reviews == sum(p.reviews for p in parts)
    assert whole.total_tokens == sum(p.total_tokens for p in parts)
    assert whole.findings == sum(p.findings for p in parts)


def test_repository_rows():
    rows = {r.repo: r for r in analytics.repositories(RECORDS, custom={A})}
    a, b = rows[A], rows[B]
    assert (a.reviews, a.passed, a.gated, a.failed, a.applied) == (3, 1, 1, 1, 1)
    assert a.findings_per_review == pytest.approx(2.5)
    assert a.custom_settings is True and b.custom_settings is False
    assert b.applied == 3  # one /apply all that applied three suggestions
    assert b.last_model == "m2"


def test_cost_needs_a_price_for_every_model():
    prices = {"m1": {"input": 1.0, "output": 5.0}}  # USD per 1M tokens
    data = analytics.overview(RECORDS, prices=prices)
    usage = {m.model: m for m in data.models}
    assert usage["m1"].cost_usd == pytest.approx((300 * 1.0 + 30 * 5.0) / 1_000_000)
    assert usage["m2"].cost_usd is None
    assert data.cost_usd is None  # a total with an unpriced model is unknown
    priced = analytics.overview(RECORDS, repo=A, prices=prices)
    assert priced.cost_usd == pytest.approx(usage["m1"].cost_usd)


def test_recent_reviews_newest_first_and_filtered():
    rows = analytics.recent_reviews(RECORDS, repo=A)
    assert [r.timestamp_ms for r in rows] == sorted((r.timestamp_ms for r in rows), reverse=True)
    assert {r.repo for r in rows} == {A}


def test_sort_rows_puts_unknown_values_last():
    rows = analytics.repositories(RECORDS)
    rows.append(analytics.RepositoryRow(repo="github/x__empty"))  # pass_rate None
    ordered = analytics.sort_rows(rows, "pass_rate", True, {"pass_rate"})
    assert ordered[-1].repo == "github/x__empty"
    assert analytics.sort_rows(rows, "not_a_column", True, {"pass_rate"}) == rows


def test_record_from_mlflow_trace_info():
    info = SimpleNamespace(
        trace_id="tr-1",
        timestamp_ms=T0,
        execution_duration=1234,
        state=SimpleNamespace(value="OK"),
        tags={"mlflow.traceName": "pr_review", "prreview.passed": "True", "other": "x"},
        trace_metadata={"pr.repo": A, "pr.number": "9", "config.model_endpoint": "m1"},
        token_usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    )
    record = analytics.record_from_info(info)
    assert (record.kind, record.repo, record.pr, record.model) == ("pr_review", A, "9", "m1")
    assert (record.total_tokens, record.duration_ms, record.state) == (7, 1234, "OK")
    assert record.tags == {"prreview.passed": "True"}


def test_cached_source_reuses_one_query():
    calls = []

    def source(start, end):
        calls.append((start, end))
        return RECORDS

    cached = analytics.CachedSource(source, ttl_seconds=60)
    cached.last_days(7)
    cached.last_days(7)
    cached.last_days(30)
    assert len(calls) == 2


def test_traces_from_before_tagging_are_not_misreported():
    """Older traces have no outcome tags: count them, but never as refused/gated."""
    old_review = TraceRecord(
        trace_id="old-r", kind=analytics.REVIEW, timestamp_ms=T0, duration_ms=1000, state="OK"
    )
    old_apply = TraceRecord(
        trace_id="old-a", kind=analytics.APPLY, timestamp_ms=T0, duration_ms=1000, state="OK"
    )
    data = analytics.overview([old_review, old_apply, review(A, passed=True)])
    assert (data.reviews, data.passed, data.gated, data.unrecorded_reviews) == (2, 1, 0, 1)
    assert data.pass_rate == 1.0
    assert dict(data.apply_outcomes) == {"outcome not recorded": 1}


def test_repository_is_derived_from_the_slug_on_older_traces():
    info = SimpleNamespace(
        trace_id="tr-old",
        timestamp_ms=T0,
        execution_duration=10,
        state="OK",
        tags={"mlflow.traceName": "pr_review"},
        trace_metadata={"pr.slug": "KabeloPhiri/pr-review-agent#9", "pr.scm": "github"},
        token_usage=None,
    )
    record = analytics.record_from_info(info)
    assert (record.repo, record.pr) == ("github/KabeloPhiri__pr-review-agent", "9")


# --- quality -------------------------------------------------------------------------


def _tagged(kind, repo, model="m1", **tags):
    return TraceRecord(
        trace_id=f"{kind}-{repo}-{len(tags)}-{sorted(tags.items())}",
        kind=kind,
        timestamp_ms=T0,
        duration_ms=10,
        state="OK",
        repo=repo,
        pr="5",
        model=model,
        tags={f"prreview.{k}": v for k, v in tags.items()},
    )


QUALITY = [
    _tagged(analytics.REVIEW, A, passed="False", rules='{"python.style": 3, "sql.select-star": 1}'),
    _tagged(analytics.REVIEW, B, model="m2", passed="True", rules='{"python.style": 1}',
            duplicate_rules='[{"file": "a.py", "line": "4", "old": "python.x", "new": "python.y"}]'),
    _tagged(analytics.FEEDBACK, A, feedback="false_positive", rule="python.style",
            finding_model="m1", fp_reason="generated", flagged_by="dev", file="a.py"),
    _tagged(analytics.APPLY, A, applied="True", rule="sql.select-star"),
    _tagged(analytics.APPLY_ALL, B, applied="True", rules='{"python.style": 1}'),
]


def test_false_positive_rate_per_rule_family_model_and_repo():
    q = analytics.quality(QUALITY)
    assert (q.posted, q.flagged, q.accepted) == (5, 1, 2)
    assert q.fp_rate == pytest.approx(1 / 5)
    by_rule = {r.key: r for r in q.by_rule}
    assert (by_rule["python.style"].posted, by_rule["python.style"].flagged) == (4, 1)
    assert by_rule["python.style"].fp_rate == pytest.approx(0.25)
    assert by_rule["sql.select-star"].accepted == 1
    assert {r.key: r.posted for r in q.by_family} == {"python": 4, "sql": 1}
    assert {r.key: r.flagged for r in q.by_model} == {"m1": 1, "m2": 0}
    assert q.by_rule[0].key == "python.style"  # most flagged first


def test_flags_and_duplicates_are_listed_and_filterable():
    q = analytics.quality(QUALITY)
    [flag] = q.flags
    assert (flag.rule, flag.reason, flag.by, flag.model) == ("python.style", "generated", "dev", "m1")
    [dup] = q.duplicates
    assert (dup.old_rule, dup.new_rule, dup.repo) == ("python.x", "python.y", B)

    only_a = analytics.quality(QUALITY, repo=A)
    assert (only_a.posted, only_a.flagged, only_a.duplicates) == (4, 1, [])


def test_bad_json_tags_are_ignored():
    record = _tagged(analytics.REVIEW, A, passed="True", rules="{not json")
    assert analytics.quality([record]).posted == 0


def test_truncation_is_judged_before_the_repository_filter(monkeypatch):
    monkeypatch.setattr(analytics, "MAX_TRACES", 4)
    records = [review(A), review(A, t=T0 + 1), review(B), review(B, t=T0 + 2)]
    assert analytics.overview(records).truncated is True
    assert analytics.overview(records, repo=A).truncated is True  # was False before


def test_applied_counts_agree_between_overview_and_repositories():
    records = [apply(B, kind=analytics.APPLY_ALL, count=3), apply(B)]
    assert analytics.overview(records).applied == 4
    [row] = analytics.repositories(records)
    assert row.applied == 4


def test_a_slug_without_a_pr_number_is_not_grouped():
    info = SimpleNamespace(
        trace_id="tr-x", timestamp_ms=T0, execution_duration=1, state="OK",
        tags={"mlflow.traceName": "pr_review"},
        trace_metadata={"pr.slug": "not-a-slug", "pr.scm": "github"}, token_usage=None,
    )
    record = analytics.record_from_info(info)
    assert (record.repo, record.pr) == ("", "")
