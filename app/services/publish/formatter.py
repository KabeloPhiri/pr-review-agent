"""Render findings as pull request comments.

Every comment carries a hidden marker so a re-run can recognise its own
previous output. The marker deliberately excludes the message text: rewording
the same finding must not produce a duplicate comment.
"""

from __future__ import annotations

import hashlib

from app.core.models import CommentDraft, Finding, ReviewResult, Severity

MARKER_PREFIX = "prreview"
SUMMARY_RULE = "summary"

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
