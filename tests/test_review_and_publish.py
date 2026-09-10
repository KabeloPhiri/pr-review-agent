import pytest

from app.core.config import resolve
from app.core.models import (
    Finding,
    GateLevel,
    PullRequest,
    PullRequestRef,
    ReviewResult,
    Severity,
    Verdict,
)
from app.services.policy.gate import evaluate
from app.services.publish.formatter import marker_for, render_finding, render_summary
from app.services.publish.publisher import Publisher
from app.services.review.base import ReviewContext
from app.services.review.chunking import build_chunks
from app.services.review.llm_reviewer import LlmReviewer
from app.services.scm.diffparse import parse_unified_diff
from app.services.scm.fake import FakeScmConnector


def _finding(**kwargs):
    defaults = dict(
        file="jobs/sales_etl.py", line=11, message="Avoid collect()", rule_id="pyspark.collect"
    )
    return Finding(**{**defaults, **kwargs})


# --- gate -----------------------------------------------------------------


def test_gate_blocks_at_threshold():
    findings = [_finding(severity=Severity.WARNING), _finding(severity=Severity.ERROR, line=12)]
    assert evaluate(findings, GateLevel.ERROR).passed is False
    assert evaluate(findings, GateLevel.ERROR).blocking_count == 1
    assert evaluate(findings, GateLevel.WARNING).blocking_count == 2
    assert evaluate(findings, GateLevel.NONE).passed is True


def test_gate_passes_with_only_info():
    assert evaluate([_finding(severity=Severity.INFO)], GateLevel.ERROR).passed is True


# --- reviewer response handling ------------------------------------------


async def _context(fixture_dir):
    from app.services.policy import standards

    scm = FakeScmConnector()
    ref = PullRequestRef(
        scm="fake", repository="r", pull_request_id="1", extra={"fixture_dir": str(fixture_dir)}
    )
    pr = await scm.get_pull_request(ref)
    config = resolve()
    bundle = await standards.load(scm, pr, config)
    return config, ReviewContext(pr=pr, config=config, standards=bundle)


async def test_llm_reply_parsing(fixture_dir):
    config, context = await _context(fixture_dir)
    diff = parse_unified_diff((fixture_dir / "diff.patch").read_text(encoding="utf-8"))
    chunk = next(c for c in build_chunks(diff, config)[0] if c.path == "jobs/sales_etl.py")
    reviewer = LlmReviewer(config, client=object())

    reply = (
        '```json\n{"findings": [{"line": 13, "severity": "ERROR", "rule_id": "pyspark.collect",'
        ' "message": "collect() pulls the whole table into the driver.",'
        ' "suggestion": "Aggregate in Spark."}]}\n```'
    )
    findings = reviewer._parse(reply, chunk, context)
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 13
    assert findings[0].source == "llm"


async def test_unparseable_reply_degrades_to_a_warning(fixture_dir):
    config, context = await _context(fixture_dir)
    diff = parse_unified_diff((fixture_dir / "diff.patch").read_text(encoding="utf-8"))
    chunk = build_chunks(diff, config)[0][0]
    reviewer = LlmReviewer(config, client=object())

    assert reviewer._parse("I think this looks fine!", chunk, context) == []
    assert any("could not be parsed" in w for w in context.warnings)


async def test_line_outside_the_diff_becomes_file_level(fixture_dir):
    config, context = await _context(fixture_dir)
    diff = parse_unified_diff((fixture_dir / "diff.patch").read_text(encoding="utf-8"))
    chunk = next(c for c in build_chunks(diff, config)[0] if c.path == "jobs/sales_etl.py")
    reviewer = LlmReviewer(config, client=object())

    far = '{"findings": [{"line": 900, "severity": "info", "rule_id": "x", "message": "m"}]}'
    assert reviewer._parse(far, chunk, context)[0].line is None

    near = '{"findings": [{"line": %d, "severity": "info", "rule_id": "x", "message": "m"}]}' % (
        min(chunk.changed_lines) - 1
    )
    assert reviewer._parse(near, chunk, context)[0].line == min(chunk.changed_lines)


# --- formatting and publishing -------------------------------------------


def test_marker_ignores_wording():
    a = _finding(message="Avoid collect()")
    b = _finding(message="collect() is unsafe here")
    assert marker_for(a) == marker_for(b)
    assert marker_for(a) != marker_for(_finding(line=99))


def test_rendered_comment_contains_its_marker():
    draft = render_finding(_finding(severity=Severity.ERROR, suggestion="Aggregate in Spark"))
    assert draft.marker in draft.body
    assert "ERROR" in draft.body
    assert "Aggregate in Spark" in draft.body


def _result(findings, passed=False):
    return ReviewResult(
        ref=PullRequestRef(repository="r", pull_request_id="1"),
        findings=findings,
        verdict=Verdict(passed=passed, gate=GateLevel.ERROR, reason="test"),
        files_reviewed=2,
    )


def test_summary_lists_file_level_findings():
    draft = render_summary(_result([_finding(line=None, message="Module has no tests")]))
    assert "Findings without a line anchor" in draft.body
    assert "Module has no tests" in draft.body


@pytest.fixture
def pr():
    return PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))


async def test_publish_is_idempotent(pr):
    scm = FakeScmConnector()
    publisher = Publisher(scm, resolve())
    result = _result([_finding(severity=Severity.ERROR)])

    first = await publisher.publish(pr, result)
    assert first == 2  # one inline comment plus the summary

    second = await publisher.publish(pr, _result([_finding(severity=Severity.ERROR)]))
    assert second == 0
    assert len(scm.posted) == 2


async def test_publish_closes_threads_for_resolved_findings(pr):
    scm = FakeScmConnector()
    publisher = Publisher(scm, resolve())
    await publisher.publish(pr, _result([_finding(severity=Severity.ERROR)]))

    await publisher.publish(pr, _result([], passed=True))
    assert scm.closed  # the stale inline thread was resolved
    assert scm.status is not None


async def test_post_comments_disabled_still_sets_status(pr):
    scm = FakeScmConnector()
    publisher = Publisher(scm, resolve(request_overrides={"post_comments": False}))
    assert await publisher.publish(pr, _result([_finding()])) == 0
    assert scm.posted == []
    assert scm.status is not None
