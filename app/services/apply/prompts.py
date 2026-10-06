"""Prompt assembly for the LLM applier.

One file in, full file out, for one or more already-identified findings —
deliberately not the chunk-based shape `app/services/review/prompts.py` uses:
this call fixes known problems in one file, not surveying a diff for new ones.
"""

from __future__ import annotations

from app.core.models import Finding

SYSTEM_RULES = """\
You are a careful senior engineer applying already-approved code review
findings to a file.

You will be given one or more findings (severity, rule, approximate line,
message, and an optional suggestion) and the file's current full content.
Produce the corrected full file content with every listed finding addressed.

Rules:
- Change only what is needed to address the listed findings. Preserve every
  other line, including unrelated formatting, comments, and whitespace,
  exactly.
- Line numbers are where the finding was raised and may have shifted since;
  locate the code the message describes.
- Apply each suggestion in full, not just its first step.
- Do not fix unrelated problems, refactor, or rename things, even if you
  notice something else wrong.
- If the suggestion already contains the fix verbatim, apply it precisely.
- If you cannot produce a safe, minimal fix for a finding — it is ambiguous,
  already fixed, or would require changes to a file you cannot see — leave
  that part of the file unchanged and still apply the others.

Return JSON only, matching exactly:
{"file_content": "<the complete corrected file, as a single string>"}
No markdown, no code fences, no commentary outside the JSON object.
"""


def build_system_prompt() -> str:
    return SYSTEM_RULES.strip()


def build_user_prompt(file: str, current_text: str, finding: Finding | list[Finding]) -> str:
    findings = finding if isinstance(finding, list) else [finding]
    lines = [f"File: {file}"]
    for number, item in enumerate(findings, start=1):
        lines += [
            "",
            f"Finding {number} of {len(findings)}",
            f"Severity: {item.severity.value}",
            f"Rule: {item.rule_id}",
            f"Line: {item.line if item.line is not None else 'unknown'}",
            f"Message: {item.message}",
        ]
        if item.suggestion:
            lines.append(f"Suggested fix:\n{item.suggestion}")
    lines.append("\nCurrent file content:\n" + current_text)
    # The literal word "json" has to appear in the *user* message: Databricks
    # rejects response_format={"type": "json_object"} with a 400 unless it
    # does — see app/services/review/prompts.py for the same note.
    lines.append("\nReturn the corrected file as the json object described above.")
    return "\n".join(lines)
