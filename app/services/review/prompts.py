"""Prompt assembly.

The system prompt is built per chunk from: the reviewer's own rules, the
standards that apply to that file's analyzers, and anything a quality tool
already reported. Keeping it per chunk (rather than one giant prompt) is what
lets a repo's PySpark standards apply to Spark files without leaking into
plain Python files.
"""

from __future__ import annotations

from app.services.review.base import ReviewContext
from app.services.review.chunking import ReviewChunk

SYSTEM_RULES = """\
You are a meticulous senior code reviewer for a data engineering team.
You review a unified diff from a pull request and report concrete defects and
standards violations in the CHANGED lines.

How to read the input:
- Each line is `<line number> <marker> | <code>`.
- `+` marks a line added by this pull request. Only these lines may be commented on.
- ` ` (space) marks unchanged context. Use it to understand the code; never comment on it.
- `-` marks a removed line. Use it to understand intent; never comment on it.
- The line number shown is the line number in the NEW version of the file. Cite it exactly.

Rules for your findings:
- Report only what you can justify from the code shown. Never speculate about
  code you cannot see, and never invent file paths or line numbers.
- One finding per problem. Do not repeat the same issue on several lines;
  report it once on the first line where it occurs.
- No praise, no summaries of what the code does, no style nits that a
  formatter would fix automatically.
- If the diff is fine, return an empty findings list. That is a good outcome.
- Prefer the repository's own standards whenever they conflict with the
  general guidance.

Severity:
- "error": a defect, data-correctness risk, security issue, or a violation the
  standards call a blocker. This can fail the build.
- "warning": a real problem that should be fixed but does not break anything.
- "info": a suggestion or a question for the author.

Return JSON only, matching exactly:
{"findings": [{"line": <int>, "end_line": <int or null>, "severity": "error|warning|info",
  "rule_id": "<short.kebab.id>", "message": "<what is wrong and why it matters>",
  "suggestion": "<concrete fix, code snippet if short>"}]}
No markdown, no code fences, no commentary outside the JSON object.
"""


def build_system_prompt(chunk: ReviewChunk, context: ReviewContext) -> str:
    parts = [SYSTEM_RULES.strip()]

    standards = context.standards.for_analyzers(chunk.analyzers)
    if standards:
        parts.append("The standards that apply to this file:\n\n" + standards)

    parts.append(
        f"Report at most {context.config.max_findings_per_file} findings for this file. "
        "If there are more, report the most severe."
    )
    return "\n\n---\n\n".join(parts)


def build_user_prompt(chunk: ReviewChunk, context: ReviewContext) -> str:
    lines = [
        f"Pull request: {context.pr.title or '(no title)'}",
        f"File: {chunk.path}",
        f"Language: {chunk.language} (analyzers: {', '.join(chunk.analyzers)})",
    ]
    if chunk.total_parts > 1:
        lines.append(f"Part {chunk.part} of {chunk.total_parts} of this file.")

    commentable = sorted(chunk.changed_lines)
    if commentable:
        lines.append(f"Lines you may comment on: {_summarize_ranges(commentable)}")

    known = context.known_issues_for(chunk.path)
    if known:
        lines.append(
            "Already reported by other tools — do not repeat these:\n"
            + "\n".join(
                f"- line {issue.line}: [{issue.rule_id}] {issue.message}" for issue in known
            )
        )

    lines.append("\nDiff:\n" + chunk.render())
    return "\n".join(lines)


def _summarize_ranges(numbers: list[int]) -> str:
    """Compact `1,2,3,7` into `1-3, 7` so long lists stay readable."""
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)
