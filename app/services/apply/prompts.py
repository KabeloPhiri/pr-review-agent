"""Prompt assembly for the LLM applier.

Single finding, full file in, full file out — deliberately not the
chunk-based shape `app/services/review/prompts.py` uses: this call fixes one
already-identified problem in one file, not surveying a diff for new ones.
"""

from __future__ import annotations

from app.core.models import Finding

SYSTEM_RULES = """\
You are a careful senior engineer applying ONE already-approved code review
finding to a file.

You will be given the finding (severity, rule, message, and an optional
suggestion) and the file's current full content. Produce the corrected full
file content.

Rules:
- Change only what is needed to address this finding. Preserve every other
  line, including unrelated formatting, comments, and whitespace, exactly.
- Do not fix unrelated problems, refactor, or rename things, even if you
  notice something else wrong.
- If the suggestion already contains the fix verbatim, apply it precisely.
- If you cannot produce a safe, minimal fix — the finding is ambiguous, or
  applying it would require changes to a file you cannot see — return the
  original content unchanged.

Return JSON only, matching exactly:
{"file_content": "<the complete corrected file, as a single string>"}
No markdown, no code fences, no commentary outside the JSON object.
"""


def build_system_prompt() -> str:
    return SYSTEM_RULES.strip()


def build_user_prompt(file: str, current_text: str, finding: Finding) -> str:
    lines = [
        f"File: {file}",
        f"Finding severity: {finding.severity.value}",
        f"Finding rule: {finding.rule_id}",
        f"Finding message: {finding.message}",
    ]
    if finding.suggestion:
        lines.append(f"Suggested fix:\n{finding.suggestion}")
    lines.append("\nCurrent file content:\n" + current_text)
    # The literal word "json" has to appear in the *user* message: Databricks
    # rejects response_format={"type": "json_object"} with a 400 unless it
    # does — see app/services/review/prompts.py for the same note.
    lines.append("\nReturn the corrected file as the json object described above.")
    return "\n".join(lines)
