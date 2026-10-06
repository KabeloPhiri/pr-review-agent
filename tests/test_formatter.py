"""`render_finding` <-> `parse_finding_comment` round trip.

This round trip is the load-bearing trick that lets `ReviewPipeline.apply`
reconstruct a `Finding` from the bot's own comment without a second state
store — if it drifts out of sync with `render_finding`'s layout, every
`/apply` reply silently stops working.
"""

from app.core.models import Finding, Severity
from app.services.publish.formatter import parse_finding_comment, render_finding


def test_round_trips_a_finding_with_a_suggestion():
    finding = Finding(
        file="jobs/sales_etl.py",
        line=11,
        severity=Severity.ERROR,
        rule_id="pyspark.collect",
        message="Avoid collect() on a full table scan.",
        suggestion="Use an aggregate instead:\n    df.agg(F.sum('amount'))",
    )
    draft = render_finding(finding)

    parsed = parse_finding_comment(draft.body)

    assert parsed == {
        "severity": "error",
        "rule_id": "pyspark.collect",
        "message": "Avoid collect() on a full table scan.",
        "suggestion": "Use an aggregate instead:\n    df.agg(F.sum('amount'))",
    }


def test_round_trips_a_finding_without_a_suggestion():
    finding = Finding(
        file="a.py", line=3, severity=Severity.INFO, rule_id="naming.vars", message="Rename this."
    )
    draft = render_finding(finding)

    parsed = parse_finding_comment(draft.body)

    assert parsed == {
        "severity": "info",
        "rule_id": "naming.vars",
        "message": "Rename this.",
        "suggestion": None,
    }


def test_round_trips_a_finding_reported_by_a_quality_tool():
    finding = Finding(
        file="a.py",
        line=3,
        severity=Severity.WARNING,
        rule_id="sonar.bug",
        message="Possible null dereference.",
        source="sonarqube",
    )
    draft = render_finding(finding)

    parsed = parse_finding_comment(draft.body)

    assert parsed["message"] == "Possible null dereference."
    assert parsed["suggestion"] is None


def test_ignores_a_comment_with_no_marker():
    assert parse_finding_comment("just a regular human comment, no marker here") is None


def test_ignores_a_comment_with_a_marker_but_no_header():
    assert parse_finding_comment("thanks!\n\n<!-- prreview:applied:abc123def456 -->") is None
