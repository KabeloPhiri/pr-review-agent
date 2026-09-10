"""End-to-end pipeline test: fixture SCM, stub reviewer, no network."""

from app.core.models import Finding, GateLevel, Severity
from app.core.pipeline import ReviewPipeline
from app.services.review.base import Reviewer, reviewer_registry


@reviewer_registry.register("stub")
class StubReviewer(Reviewer):
    """Reports one finding per chunk, anchored to the first changed line."""

    name = "stub"
    calls: list[str] = []

    async def review(self, chunks, context):
        StubReviewer.calls = [chunk.path for chunk in chunks]
        findings = []
        for chunk in chunks:
            line = min(chunk.changed_lines) if chunk.changed_lines else None
            severity = Severity.ERROR if chunk.language == "python" else Severity.WARNING
            findings.append(
                Finding(
                    file=chunk.path,
                    line=line,
                    severity=severity,
                    rule_id=f"stub.{chunk.language}",
                    message=f"stub finding for {chunk.path}",
                    language=chunk.language,
                )
            )
        return findings


async def test_review_runs_end_to_end(ref):
    result = await ReviewPipeline().run(ref, overrides={"reviewer": "stub"})

    assert {f.file for f in result.findings} == {"jobs/sales_etl.py", "sql/daily_totals.sql"}
    assert result.files_reviewed == 2
    assert result.published is True
    assert result.comments_posted == 3  # two inline comments plus the summary
    # The repo's .prreview/config.yaml sets the gate to `warning`.
    assert result.verdict.gate is GateLevel.WARNING
    assert result.verdict.passed is False
    assert result.verdict.blocking_count == 2


async def test_repo_standards_reach_the_reviewer(ref, monkeypatch):
    captured = {}

    async def capture(self, chunks, context):
        captured["standards"] = context.standards.for_analyzers(["python", "pyspark"])
        captured["sources"] = context.standards.sources
        return []

    monkeypatch.setattr(StubReviewer, "review", capture)
    await ReviewPipeline().run(ref, overrides={"reviewer": "stub"})

    assert ".prreview/standards.md" in captured["sources"]
    assert "Contoso data platform standards" in captured["standards"]
    assert "authoritative" in captured["standards"]
    assert "PySpark standards" in captured["standards"]  # bundled defaults still present


async def test_publish_can_be_disabled(ref):
    result = await ReviewPipeline().run(ref, overrides={"reviewer": "stub"}, publish=False)
    assert result.published is False
    assert result.comments_posted == 0
    assert result.findings


async def test_request_overrides_beat_repo_config(ref):
    result = await ReviewPipeline().run(
        ref, overrides={"reviewer": "stub", "severity_gate": "none"}
    )
    assert result.verdict.gate is GateLevel.NONE
    assert result.verdict.passed is True


async def test_only_supported_languages_are_reviewed(ref):
    await ReviewPipeline().run(ref, overrides={"reviewer": "stub", "analyzers": ["sql"]})
    assert StubReviewer.calls == ["sql/daily_totals.sql"]
