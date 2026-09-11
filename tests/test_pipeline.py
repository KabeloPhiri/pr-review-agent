"""End-to-end pipeline test: fixture SCM, stub reviewer, no network."""

import pytest

from app.core.errors import ReviewerError
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


async def test_total_review_failure_does_not_pass_the_pull_request(ref, monkeypatch):
    """A model outage must fail loudly, never green-light every pull request."""

    async def all_chunks_fail(self, chunks, context):
        context.failed_chunks = len(chunks)
        context.warnings.extend(f"{c.path}: review failed (endpoint down)" for c in chunks)
        return []

    monkeypatch.setattr(StubReviewer, "review", all_chunks_fail)

    with pytest.raises(ReviewerError) as exc:
        await ReviewPipeline().run(ref, overrides={"reviewer": "stub"})
    assert "could not review any" in str(exc.value)


async def test_partial_review_failure_still_reports(ref, monkeypatch):
    async def one_chunk_fails(self, chunks, context):
        context.failed_chunks = 1
        context.warnings.append("half the review failed")
        return [Finding(file=chunks[0].path, line=min(chunks[0].changed_lines), message="found it")]

    monkeypatch.setattr(StubReviewer, "review", one_chunk_fails)
    result = await ReviewPipeline().run(ref, overrides={"reviewer": "stub"})
    assert len(result.findings) == 1
    assert "half the review failed" in result.warnings


async def test_only_supported_languages_are_reviewed(ref):
    await ReviewPipeline().run(ref, overrides={"reviewer": "stub", "analyzers": ["sql"]})
    assert StubReviewer.calls == ["sql/daily_totals.sql"]


async def test_scm_token_never_reaches_the_trace(ref, monkeypatch):
    """The SCM token is a caller credential; a trace is durable storage.

    `@mlflow.trace` captures every argument automatically, so without an
    explicit redaction the PAT is written to the MLflow experiment in
    plaintext on every review.
    """
    captured = {}

    class _Span:
        def set_inputs(self, value):
            captured["inputs"] = value

    monkeypatch.setattr("app.core.pipeline.mlflow.get_current_active_span", lambda: _Span())

    await ReviewPipeline().run(
        ref, scm_token="ghp_supersecrettoken", overrides={"reviewer": "stub"}, publish=False
    )

    assert captured["inputs"]["scm_token"] == "(redacted)"
    assert "ghp_supersecrettoken" not in str(captured["inputs"])


async def test_all_replies_unparseable_does_not_pass_the_pull_request(ref, monkeypatch):
    """A garbled model is an outage in disguise, not a clean bill of health."""

    async def every_reply_is_junk(self, chunks, context):
        for chunk in chunks:
            context.warnings.append(f"{chunk.path}: model reply could not be parsed")
            context.failed_chunks += 1
        return []

    monkeypatch.setattr(StubReviewer, "review", every_reply_is_junk)

    with pytest.raises(ReviewerError) as exc:
        await ReviewPipeline().run(ref, overrides={"reviewer": "stub"})
    assert "could not review any" in str(exc.value)
