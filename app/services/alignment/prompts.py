"""Prompt assembly for the LLM alignment checker.

Whole pull request in, one verdict out — the requirement is judged across
every changed file at once, not chunk by chunk like a review.
"""

from __future__ import annotations

from app.core.settings_store import get_settings_store
from app.services.alignment.base import AlignmentInput

#: What the model is told about the task and how to judge it. Admins can
#: replace this from the console (prompt name `alignment_guidance`).
GUIDANCE = """\
You are a senior engineer checking whether a pull request delivers what it
was raised for. You are given the requirement (or acceptance criteria) the
requester pasted, the kind of change it is meant to be, the pull request's
title and description, and the full diff.

How to read the diff:
- Each line is `<line number> <marker> | <code>`.
- `+` marks an added line, ` ` (space) unchanged context, `-` a removed line.
- The line number is the line in the NEW version of the file. Cite it exactly.

How to judge:
- Split the requirement into the separate things it asks for. Judge each one
  only from the code shown: "met", "partial" (started, or only some cases),
  or "missing". Cite evidence as `path:line`.
- Never assume code you cannot see does the work. If the requirement depends
  on something outside the diff, say so in the criterion's note and mark it
  "partial" or "missing", not "met".
- List changes the requirement does not explain under unrelated changes —
  refactors, unrelated fixes, debug leftovers. Formatting-only churn in a
  touched file is not worth listing.
- Say whether the diff looks like the declared kind of change. A "bug fix"
  that adds a new public feature, or a "refactor" that changes behaviour, does
  not match.
- Overall verdict: "aligned" when every criterion is met and nothing
  significant is unrelated; "partial" when some are met; "not_aligned" when
  the diff does something else; "unclear" only when the requirement is too
  vague to check against.
- Be concise and specific. No praise.
"""

#: The reply format the parser depends on. Always appended by code and never
#: editable, so a guidance edit cannot break parsing.
OUTPUT_CONTRACT = """\
Return JSON only, matching exactly:
{"verdict": "aligned|partial|not_aligned|unclear",
 "summary": "<two or three sentences>",
 "criteria": [{"text": "<one thing the requirement asks for>",
   "status": "met|partial|missing", "evidence": ["<path:line>"], "note": "<short>"}],
 "unrelated_changes": [{"file": "<path>", "reason": "<short>"}],
 "intent_matches": <true|false>,
 "detected_intent": "requirement_change|change_ticket|feature|bug_fix|refactor|other"}
No markdown, no code fences, no commentary outside the JSON object.
"""

SYSTEM_RULES = GUIDANCE.strip() + "\n\n" + OUTPUT_CONTRACT.strip()


def build_system_prompt() -> str:
    """Guidance (the admin console's, if set) followed by the fixed contract."""
    guidance = get_settings_store().prompt("alignment_guidance") or GUIDANCE
    return guidance.strip() + "\n\n" + OUTPUT_CONTRACT.strip()


def build_user_prompt(request: AlignmentInput) -> str:
    pr = request.pr
    lines = [
        f"Declared kind of change: {request.intent.label} ({request.intent.value})",
        "",
        "Requirement:",
        request.requirement.strip(),
        "",
        f"Pull request title: {pr.title or '(no title)'}",
        "Pull request description:",
        (pr.description or "(none)").strip(),
        "",
        f"Changed files ({len(request.files)}): " + ", ".join(request.files),
    ]
    if request.omitted:
        lines.append(
            "Not shown (binary, excluded or over the size limit): " + ", ".join(request.omitted)
        )
    lines.append("\nDiff:\n" + request.diff_text)
    # The literal word "json" has to appear in the *user* message: Databricks
    # rejects response_format={"type": "json_object"} with a 400 unless it
    # does — see app/services/review/prompts.py for the same note.
    lines.append("\nReturn your assessment as the json object described above.")
    return "\n".join(lines)
