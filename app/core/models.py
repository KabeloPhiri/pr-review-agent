"""Domain model shared by every service.

These types are provider-agnostic on purpose: an SCM connector translates
whatever its API returns into `PullRequest` / `Diff`, and everything
downstream only ever sees these.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2}[self.value]


class GateLevel(str, Enum):
    """Minimum severity that fails the pull request."""

    ERROR = "error"
    WARNING = "warning"
    NONE = "none"


# ---------------------------------------------------------------------------
# Pull request + diff
# ---------------------------------------------------------------------------


class PullRequestRef(BaseModel):
    """Everything an SCM connector needs to address one pull request.

    `organization` / `project` are Azure DevOps concepts; other providers
    leave them unset and use `repository` alone. `extra` carries
    provider-specific addressing without widening this model.
    """

    scm: str = "azure_devops"
    organization: str | None = None
    project: str | None = None
    repository: str
    pull_request_id: str
    base_url: str | None = Field(
        default=None, description="Override for on-prem or self-hosted SCM servers"
    )
    extra: dict[str, str] = Field(default_factory=dict)

    def slug(self) -> str:
        return self.repo_slug() + "#" + str(self.pull_request_id)

    def repo_slug(self) -> str:
        """The repository this pull request belongs to, without the PR number."""
        return "/".join(p for p in (self.organization, self.project, self.repository) if p)


class PullRequest(BaseModel):
    ref: PullRequestRef
    title: str = ""
    description: str = ""
    author: str = ""
    source_branch: str = ""
    target_branch: str = ""
    source_commit: str = ""
    target_commit: str = ""
    url: str | None = None
    is_draft: bool = False
    #: The source branch lives in a different repository (a fork). Pushing to
    #: it needs access this app's token does not have, so apply refuses it.
    is_fork: bool = False


class ChangeType(str, Enum):
    ADD = "add"
    EDIT = "edit"
    DELETE = "delete"
    RENAME = "rename"


class Hunk(BaseModel):
    """One unified-diff hunk.

    `lines` keeps the raw ' ', '+' and '-' prefixes so the reviewer sees
    context and removals, while `new_start` anchors added lines to real file
    line numbers, which is what inline PR comments need.
    """

    old_start: int = 1
    old_lines: int = 0
    new_start: int = 1
    new_lines: int = 0
    header: str = ""
    lines: list[str] = Field(default_factory=list)

    def changed_line_numbers(self) -> set[int]:
        """New-file line numbers added by this hunk."""
        numbers: set[int] = set()
        line_no = self.new_start
        for raw in self.lines:
            if raw.startswith("+"):
                numbers.add(line_no)
                line_no += 1
            elif raw.startswith("-"):
                continue
            else:
                line_no += 1
        return numbers

    def size(self) -> int:
        return sum(len(line) for line in self.lines)


class DiffFile(BaseModel):
    path: str
    previous_path: str | None = None
    change_type: ChangeType = ChangeType.EDIT
    hunks: list[Hunk] = Field(default_factory=list)
    is_binary: bool = False
    language: str | None = None

    def changed_line_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for hunk in self.hunks:
            numbers |= hunk.changed_line_numbers()
        return numbers

    def size(self) -> int:
        return sum(hunk.size() for hunk in self.hunks)


class Diff(BaseModel):
    files: list[DiffFile] = Field(default_factory=list)

    def size(self) -> int:
        return sum(f.size() for f in self.files)


# ---------------------------------------------------------------------------
# Findings and result
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    file: str
    line: int | None = None
    end_line: int | None = None
    severity: Severity = Severity.WARNING
    rule_id: str = "general"
    message: str
    suggestion: str | None = None
    source: str = "llm"
    language: str | None = None
    #: Serving endpoint that produced the finding; recorded in the comment so
    #: false-positive feedback can be attributed to the model that wrote it.
    model: str | None = None

    def dedupe_key(self) -> tuple[str, int | None, str]:
        return (self.file, self.line, self.rule_id.strip().lower())


class CommentDraft(BaseModel):
    """A comment the publisher wants to exist on the pull request.

    `marker` is a stable hidden id embedded in the body so a re-run can tell
    "already said this" from "new finding" without a database.
    """

    body: str
    marker: str
    file: str | None = None
    line: int | None = None
    end_line: int | None = None
    is_summary: bool = False


class ExistingComment(BaseModel):
    """A comment thread already on the pull request, as seen by a connector."""

    thread_id: str
    marker: str | None = None
    is_closed: bool = False
    file: str | None = None
    line: int | None = None
    #: Filled where the connector has them for free; `/apply all` needs them
    #: to rebuild each suggestion, the idempotency scan does not.
    body: str = ""
    author: str = ""
    #: Provider-native id for `get_comment`/`reply_to_comment` (GitHub's
    #: plain REST id), as opposed to `thread_id`.
    comment_id: str | None = None


class SourceComment(BaseModel):
    """One existing comment fetched by id, body included.

    Richer than `ExistingComment` (which only exists for the idempotency
    scan): this is what `ReviewPipeline.apply` needs to reconstruct the
    original `Finding` from the bot's own rendered comment text.
    """

    thread_id: str
    body: str
    file: str | None = None
    line: int | None = None
    author: str = ""


class Verdict(BaseModel):
    passed: bool
    gate: GateLevel
    blocking_count: int = 0
    reason: str = ""


class ReviewResult(BaseModel):
    ref: PullRequestRef
    findings: list[Finding] = Field(default_factory=list)
    verdict: Verdict
    files_reviewed: int = 0
    files_skipped: list[str] = Field(default_factory=list)
    comments_posted: int = 0
    published: bool = False
    trace_id: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts


class ApplyResult(BaseModel):
    """Outcome of accepting one bot suggestion and pushing it as a commit.

    `applied=False` with a `reason` is a soft, expected outcome (disabled by
    config, wrong requester, stale/foreign comment) — not an error. A real
    failure (the push itself failing, the model erroring) raises instead, the
    same asymmetry `ReviewResult`/`ReviewerError` already apply to `run()`.
    """

    ref: PullRequestRef
    applied: bool
    reason: str | None = None
    file: str | None = None
    commit_sha: str | None = None
    #: Post-push follow-ups (confirmation comment, resolving the thread) that
    #: failed. The commit is already on the branch, so these do not fail it.
    warnings: list[str] = Field(default_factory=list)


class SkippedSuggestion(BaseModel):
    comment_id: str
    reason: str


class BulkApplyResult(BaseModel):
    """Outcome of `/apply all`: every open bot suggestion, one commit per file.

    `reason` is set only when the whole request was refused (disabled, wrong
    requester, nothing to apply); per-suggestion outcomes are in `skipped`.
    """

    ref: PullRequestRef
    applied: bool
    reason: str | None = None
    applied_comment_ids: list[str] = Field(default_factory=list)
    commit_shas: list[str] = Field(default_factory=list)
    skipped: list[SkippedSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FeedbackResult(BaseModel):
    """Outcome of `/fp`: one bot finding flagged as a false positive.

    Like `ApplyResult`, a refusal is soft (`recorded=False` with a `reason`).
    What was recorded lives on the `pr_feedback` trace, which the admin
    console aggregates into false-positive rates.
    """

    ref: PullRequestRef
    recorded: bool
    reason: str | None = None
    rule_id: str | None = None
    file: str | None = None
    model: str | None = None
    warnings: list[str] = Field(default_factory=list)
