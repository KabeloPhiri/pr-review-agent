"""Render findings as pull request comments.

Every comment carries a hidden marker so a re-run can recognise its own
previous output. The marker deliberately excludes the message text: rewording
the same finding must not produce a duplicate comment.
"""

from __future__ import annotations

import hashlib
import re

from app.core.models import CommentDraft, Finding, ReviewResult, Severity

MARKER_PREFIX = "prreview"
SUMMARY_RULE = "summary"

_HEADER_RE = re.compile(r"\*\*(ERROR|WARNING|INFO)\*\*\s*·\s*`([^`]+)`\s*$")
_SUGGESTION_HEADING = "**Suggestion**"
_REPORTED_BY_RE = re.compile(r"^_Reported by .+\._$")
_MARKER_LINE_RE = re.compile(r"<!--\s*prreview:[^\s>]+\s*-->")
#: Second hidden marker naming the model, placed after the finding's own
#: marker so every "first marker in the body" lookup still sees the finding.
_MODEL_RE = re.compile(r"<!--\s*prreview:model:([^\s>]+)\s*-->")

_EMOJI = {
    Severity.ERROR: "🔴",
    Severity.WARNING: "🟠",
    Severity.INFO: "🔵",
}


def marker_for(finding: Finding) -> str:
    digest = hashlib.sha1(
        f"{finding.file}|{finding.line}|{finding.rule_id.lower()}".encode()
    ).hexdigest()[:12]
    return f"{MARKER_PREFIX}:{digest}"


def summary_marker(result: ReviewResult) -> str:
    """Changes when the outcome changes, so an unchanged re-run stays quiet."""
    counts = result.counts_by_severity()
    payload = f"{result.verdict.passed}|{counts}|{len(result.findings)}"
    digest = hashlib.sha1(payload.encode()).hexdigest()[:12]
    return f"{MARKER_PREFIX}:{SUMMARY_RULE}:{digest}"


def render_finding(finding: Finding) -> CommentDraft:
    emoji = _EMOJI.get(finding.severity, "")
    lines = [
        f"{emoji} **{finding.severity.value.upper()}** · `{finding.rule_id}`",
        "",
        finding.message,
    ]
    if finding.suggestion:
        lines += ["", "**Suggestion**", "", finding.suggestion]
    if finding.source != "llm":
        lines += ["", f"_Reported by {finding.source}._"]
    lines += ["", f"<!-- {marker_for(finding)} -->"]
    if finding.model:
        lines.append(f"<!-- {MARKER_PREFIX}:model:{finding.model} -->")

    return CommentDraft(
        body="\n".join(lines),
        marker=marker_for(finding),
        file=finding.file,
        line=finding.line,
        end_line=finding.end_line,
    )


def render_summary(result: ReviewResult) -> CommentDraft:
    counts = result.counts_by_severity()
    verdict = "✅ Passed" if result.verdict.passed else "❌ Changes requested"

    lines = [
        "## AI code review",
        "",
        f"**{verdict}** — {result.verdict.reason}",
        "",
        f"| Severity | Count |\n| --- | --- |\n"
        f"| 🔴 Error | {counts['error']} |\n"
        f"| 🟠 Warning | {counts['warning']} |\n"
        f"| 🔵 Info | {counts['info']} |",
        "",
        f"Reviewed {result.files_reviewed} file(s).",
    ]

    file_level = [f for f in result.findings if f.line is None]
    if file_level:
        lines += ["", "### Findings without a line anchor", ""]
        lines += [
            f"- `{f.file}` · **{f.severity.value}** · `{f.rule_id}` — {f.message}"
            for f in file_level
        ]

    if result.files_skipped:
        shown = result.files_skipped[:10]
        lines += ["", "<details><summary>Skipped files</summary>", ""]
        lines += [f"- {item}" for item in shown]
        if len(result.files_skipped) > len(shown):
            lines.append(f"- …and {len(result.files_skipped) - len(shown)} more")
        lines += ["", "</details>"]

    if result.warnings:
        lines += ["", "> ⚠️ " + "; ".join(result.warnings[:5])]

    if result.trace_id:
        lines += ["", f"_MLflow trace: `{result.trace_id}`_"]

    lines += ["", f"<!-- {summary_marker(result)} -->"]

    return CommentDraft(body="\n".join(lines), marker=summary_marker(result), is_summary=True)


def parse_finding_comment(body: str) -> dict | None:
    """Reconstruct a finding's fields from a `render_finding` comment body.

    The exact inverse of `render_finding` — this is what lets
    `ReviewPipeline.apply` recover a `Finding` from a comment the PR author
    accepted without a second state store: the PR's own comment *is* the
    state. Returns `None` when `body` is not recognisably one of this bot's
    own finding comments (no marker, or no header line), which callers treat
    as a soft "not a bot suggestion" outcome.
    """
    if not _MARKER_LINE_RE.search(body):
        return None

    lines = body.splitlines()
    if len(lines) < 3:
        return None

    header = _HEADER_RE.search(lines[0])
    if not header:
        return None
    severity, rule_id = header.group(1).lower(), header.group(2)

    suggestion_idx = _index_of(lines, lambda line: line == _SUGGESTION_HEADING)
    reported_idx = _index_of(lines, lambda line: bool(_REPORTED_BY_RE.match(line)))
    marker_idx = _index_of(lines, lambda line: bool(_MARKER_LINE_RE.search(line)))

    message_end = suggestion_idx if suggestion_idx is not None else reported_idx
    if message_end is None:
        message_end = marker_idx
    message = _strip_block(lines[2:message_end])
    if not message:
        return None

    suggestion = None
    if suggestion_idx is not None:
        suggestion_end = reported_idx if reported_idx is not None else marker_idx
        suggestion = _strip_block(lines[suggestion_idx + 1 : suggestion_end]) or None

    model = _MODEL_RE.search(body)
    return {
        "severity": severity,
        "rule_id": rule_id,
        "message": message,
        "suggestion": suggestion,
        "model": model.group(1) if model else None,
    }


def _index_of(lines: list[str], predicate) -> int | None:
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    return None


def _strip_block(lines: list[str]) -> str:
    start, end = 0, len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])
