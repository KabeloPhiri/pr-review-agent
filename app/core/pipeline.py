"""The review pipeline: the one place that knows the order of operations.

    fetch PR -> load config + standards -> fetch diff -> quality issues
    -> chunk -> review -> merge/dedupe -> gate -> publish

Every step goes through a plugin interface, so swapping Azure DevOps for
GitHub, or the single-call reviewer for an agent, changes configuration and
one module — never this file's shape.
"""

from __future__ import annotations

import logging
from typing import Any

import mlflow

from app.core.config import EffectiveConfig, parse_repo_config, resolve
from app.core.errors import PrReviewError, ReviewerError, ScmConflictError, ScmError
from app.core.models import (
    ApplyResult,
    BulkApplyResult,
    CommentDraft,
    Diff,
    ExistingComment,
    Finding,
    PullRequest,
    PullRequestRef,
    ReviewResult,
    Severity,
    SkippedSuggestion,
)
from app.core.settings_store import RepoKey
from app.services.apply import get_applier
from app.services.policy import gate, standards
from app.services.publish.formatter import parse_finding_comment
from app.services.publish.publisher import Publisher
from app.services.quality import get_quality_connector
from app.services.review import ReviewContext, get_reviewer
from app.services.review.chunking import build_chunks
from app.services.scm import ScmConnector, get_connector

logger = logging.getLogger(__name__)

#: How many times `apply()` re-reads the branch and regenerates a fix when the
#: push is refused because the file changed meanwhile — typically another
#: `/apply` on the same file landing first.
APPLY_MAX_ATTEMPTS = 3

#: What the bot says in the suggestion's thread when it does not apply it.
#: Only for refusals after the requester and comment are verified — replying
#: to arbitrary commenters would let anyone make the bot post.
_NOT_APPLIED_REPLIES = {
    "no_change_generated": (
        "ℹ️ Not applied: no change was needed — this may already be fixed on the branch."
    ),
    "file_changed_since_review": (
        "⚠️ Not applied: the file kept changing while this fix was being prepared "
        f"({APPLY_MAX_ATTEMPTS} attempts). Reply `/apply` again once other pushes settle."
    ),
}


class ReviewPipeline:
    """Stateless orchestrator; construct one per request."""

    async def resolve_config(
        self,
        scm: ScmConnector,
        pr: PullRequest,
        overrides: dict[str, Any] | None,
    ) -> EffectiveConfig:
        """Layer repo config over defaults and env, then request overrides."""
        bootstrap = resolve(request_overrides=overrides)
        raw = await scm.get_file_text(pr, bootstrap.config_path)
        return resolve(repo_config=parse_repo_config(raw), request_overrides=overrides)

    @mlflow.trace(name="pr_review")
    async def run(
        self,
        ref: PullRequestRef,
        *,
        scm_token: str | None = None,
        overrides: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> ReviewResult:
        _redact_trace_inputs(ref, overrides, publish=publish)
        scm = get_connector(ref.scm, token=scm_token)
        try:
            pr = await scm.get_pull_request(ref)
            config = await self.resolve_config(scm, pr, overrides)
            _tag_trace(pr, config)

            standards_bundle = await standards.load(scm, pr, config)
            diff = await scm.get_diff(pr)
            findings, chunks_reviewed, skipped, warnings = await self._analyze(
                scm, pr, diff, config, standards_bundle
            )

            verdict = gate.evaluate(findings, config.severity_gate)
            result = ReviewResult(
                ref=ref,
                findings=findings,
                verdict=verdict,
                files_reviewed=len(set(chunks_reviewed)),
                files_skipped=skipped,
                warnings=warnings,
                trace_id=_current_trace_id(),
            )

            if publish:
                await Publisher(scm, config).publish(pr, result)
            counts = result.counts_by_severity()
            _tag_outcome(
                {
                    "prreview.passed": verdict.passed,
                    "prreview.findings": len(findings),
                    "prreview.errors": counts["error"],
                    "prreview.warnings": counts["warning"],
                    "prreview.infos": counts["info"],
                    "prreview.files_reviewed": result.files_reviewed,
                    "prreview.published": publish,
                }
            )
            return result
        finally:
            await scm.aclose()

    @mlflow.trace(name="pr_apply")
    async def apply(
        self,
        ref: PullRequestRef,
        *,
        scm_token: str | None = None,
        comment_id: str,
        requester: str,
        overrides: dict[str, Any] | None = None,
    ) -> ApplyResult:
        """Accept one bot suggestion and push it as a commit.

        `applied=False` with a `reason` is the normal outcome for every
        "this wasn't meant for us" case (disabled, wrong requester, stale or
        foreign comment, nothing to change) — only a real SCM/model failure
        raises, mirroring `run()`'s own failure policy.
        """
        _redact_trace_inputs(ref, overrides, comment_id=comment_id, requester=requester)
        result = await self._apply(
            ref,
            scm_token=scm_token,
            comment_id=comment_id,
            requester=requester,
            overrides=overrides,
        )
        _tag_outcome({"prreview.applied": result.applied, "prreview.reason": result.reason})
        return result

    async def _apply(
        self,
        ref: PullRequestRef,
        *,
        scm_token: str | None,
        comment_id: str,
        requester: str,
        overrides: dict[str, Any] | None,
    ) -> ApplyResult:
        scm = get_connector(ref.scm, token=scm_token)
        try:
            pr = await scm.get_pull_request(ref)
            # Pushing code is the repository's decision, not the pull
            # request's: read config from the *target* branch, so a PR cannot
            # opt itself in (or widen apply_trusted_authors) from its own
            # .prreview/config.yaml.
            config = await self.resolve_config(
                scm, pr.model_copy(update={"source_commit": pr.target_commit}), overrides
            )
            _tag_trace(pr, config)

            refusal = _apply_refusal(config, pr, requester)
            if refusal is not None:
                return ApplyResult(ref=ref, applied=False, reason=refusal)

            source = await scm.get_comment(pr, comment_id)
            if source is None or source.file is None:
                return ApplyResult(
                    ref=ref, applied=False, reason="comment_not_found_or_unsupported"
                )
            finding = _bot_finding(config, source.author, source.body, source.file, source.line)
            if finding is None:
                return ApplyResult(ref=ref, applied=False, reason="not_a_bot_suggestion")

            # Keyed by the accepted comment, so a second `/apply` (or one
            # after `/apply all`) or a redelivered webhook cannot apply twice.
            applied_marker = _applied_marker(comment_id)
            if _already_applied(await scm.list_comments(pr), comment_id):
                return ApplyResult(ref=ref, applied=False, reason="already_applied")

            applier = get_applier(config.applier, config)
            try:
                commit_sha, reason = await self._push_fix(scm, ref, pr, applier, [finding])
            except PrReviewError as exc:
                # Without this the only trace of a failure is a red Actions run.
                await _reply_quietly(
                    scm, pr, comment_id, f"❌ Could not apply this suggestion: {exc.message}"
                )
                raise
            finally:
                await applier.aclose()
            if reason is not None:
                await _reply_quietly(scm, pr, comment_id, _NOT_APPLIED_REPLIES[reason])
                return ApplyResult(ref=ref, applied=False, reason=reason)

            # The commit is on the branch now. Anything below failing is a
            # warning, not a failure — reporting a landed fix as failed only
            # invites a retry.
            warnings: list[str] = []
            try:
                await scm.post_comment(
                    pr,
                    CommentDraft(
                        body=(
                            f"✅ Applied in commit `{commit_sha[:12]}`.\n\n"
                            f"<!-- {applied_marker} -->"
                        ),
                        marker=applied_marker,
                    ),
                )
            except ScmError as exc:
                logger.warning("Applied %s but could not confirm it", commit_sha, exc_info=True)
                warnings.append(f"could not post the confirmation comment ({exc.message})")
            try:
                await scm.close_comment(pr, source.thread_id)
            except ScmError as exc:
                logger.warning("Applied %s but could not resolve the thread", commit_sha)
                warnings.append(f"could not resolve the suggestion thread ({exc.message})")

            return ApplyResult(
                ref=ref,
                applied=True,
                file=source.file,
                commit_sha=commit_sha,
                warnings=warnings,
            )
        finally:
            await scm.aclose()

    @mlflow.trace(name="pr_apply_all")
    async def apply_all(
        self,
        ref: PullRequestRef,
        *,
        scm_token: str | None = None,
        requester: str,
        overrides: dict[str, Any] | None = None,
    ) -> BulkApplyResult:
        """Accept every open bot suggestion on the pull request at once.

        Same gates as `apply()`. Suggestions are grouped by file and each file
        gets one model call and one commit, so fixes in the same file cannot
        collide the way simultaneous single `/apply` replies can. A file that
        fails is reported in `skipped` and the others still land; only when
        nothing could be applied because of a real failure does this raise.
        """
        _redact_trace_inputs(ref, overrides, requester=requester, apply_all=True)
        result = await self._apply_all(
            ref, scm_token=scm_token, requester=requester, overrides=overrides
        )
        _tag_outcome(
            {
                "prreview.applied": result.applied,
                "prreview.reason": result.reason,
                "prreview.applied_count": len(result.applied_comment_ids),
                "prreview.skipped_count": len(result.skipped),
            }
        )
        return result

    async def _apply_all(
        self,
        ref: PullRequestRef,
        *,
        scm_token: str | None,
        requester: str,
        overrides: dict[str, Any] | None,
    ) -> BulkApplyResult:
        scm = get_connector(ref.scm, token=scm_token)
        try:
            pr = await scm.get_pull_request(ref)
            # Target branch, as in `apply()`: a PR cannot opt itself in.
            config = await self.resolve_config(
                scm, pr.model_copy(update={"source_commit": pr.target_commit}), overrides
            )
            _tag_trace(pr, config)

            refusal = _apply_refusal(config, pr, requester)
            if refusal is not None:
                return BulkApplyResult(ref=ref, applied=False, reason=refusal)

            comments = await scm.list_comments(pr)
            by_file: dict[str, list[tuple[ExistingComment, Finding]]] = {}
            skipped: list[SkippedSuggestion] = []
            for comment in comments:
                # Inline, still anchored (GitHub drops the line once the code
                # moved on), and one of the bot's findings with a suggestion.
                if comment.is_closed or not (comment.file and comment.comment_id):
                    continue
                finding = _bot_finding(
                    config, comment.author, comment.body, comment.file, comment.line
                )
                if finding is None or not finding.suggestion:
                    continue
                if _already_applied(comments, comment.comment_id):
                    skipped.append(
                        SkippedSuggestion(comment_id=comment.comment_id, reason="already_applied")
                    )
                    continue
                by_file.setdefault(comment.file, []).append((comment, finding))

            if not by_file:
                return BulkApplyResult(
                    ref=ref, applied=False, reason="nothing_to_apply", skipped=skipped
                )

            applied: list[tuple[ExistingComment, Finding, str]] = []
            commit_shas: list[str] = []
            last_error: PrReviewError | None = None
            applier = get_applier(config.applier, config)
            try:
                for file, items in by_file.items():
                    try:
                        commit_sha, reason = await self._push_fix(
                            scm, ref, pr, applier, [finding for _, finding in items]
                        )
                    except PrReviewError as exc:
                        logger.warning("/apply all failed on %s", file, exc_info=True)
                        last_error = exc
                        commit_sha, reason = None, f"failed: {exc.message}"
                    if commit_sha is None:
                        skipped += [
                            SkippedSuggestion(comment_id=c.comment_id, reason=reason)
                            for c, _ in items
                        ]
                        continue
                    commit_shas.append(commit_sha)
                    applied += [(c, f, commit_sha) for c, f in items]
                    # The next file is read at the new head.
                    pr = await scm.get_pull_request(ref)
            finally:
                await applier.aclose()

            if not applied and last_error is not None:
                raise last_error

            warnings: list[str] = []
            if applied:
                try:
                    await scm.post_comment(pr, _bulk_confirmation(applied, skipped))
                except ScmError as exc:
                    logger.warning("Applied %d fixes but could not confirm", len(applied))
                    warnings.append(f"could not post the confirmation comment ({exc.message})")
                for comment, _, _ in applied:
                    try:
                        await scm.close_comment(pr, comment.thread_id)
                    except ScmError as exc:
                        warnings.append(f"could not resolve {comment.thread_id} ({exc.message})")

            return BulkApplyResult(
                ref=ref,
                applied=bool(applied),
                reason=None if applied else "no_change_generated",
                applied_comment_ids=[c.comment_id for c, _, _ in applied],
                commit_shas=commit_shas,
                skipped=skipped,
                warnings=warnings,
            )
        finally:
            await scm.aclose()

    async def _push_fix(
        self,
        scm: ScmConnector,
        ref: PullRequestRef,
        pr: PullRequest,
        applier: Any,
        findings: list[Finding],
    ) -> tuple[str, None] | tuple[None, str]:
        """Fix `findings` (all in one file) in one commit: `(commit_sha, None)`
        or `(None, reason)`.

        A push is refused (`ScmConflictError`) when the file changed after
        `pr` was read — usually another `/apply` on the same file. Pushing
        the stale patch would revert that change, so re-read the branch and
        regenerate from the new content instead; several `/apply` replies in
        quick succession then land one after another rather than all but
        one being refused.
        """
        file = findings[0].file
        rules = ", ".join(dict.fromkeys(f.rule_id for f in findings))
        message = (
            f"Apply AI review suggestion ({rules}) on {file}"
            if len(findings) == 1
            else f"Apply {len(findings)} AI review suggestions on {file}\n\nRules: {rules}"
        )
        for attempt in range(1, APPLY_MAX_ATTEMPTS + 1):
            if attempt > 1:
                pr = await scm.get_pull_request(ref)
            current_text = await scm.get_file_text(pr, file)
            if current_text is None:
                raise ScmError(f"Could not read the current content of {file}")

            if len(findings) == 1:
                new_content = await applier.generate_patch(
                    file=file, current_text=current_text, finding=findings[0]
                )
            else:
                new_content = await applier.generate_patch_many(
                    file=file, current_text=current_text, findings=findings
                )
            if new_content:
                new_content = _match_line_endings(current_text, new_content)
            if not new_content or new_content == current_text:
                return None, "no_change_generated"

            try:
                commit_sha = await scm.update_file(pr, file, new_content, message=message)
                return commit_sha, None
            except ScmConflictError:
                logger.info(
                    "%s changed while applying %s (attempt %d/%d)",
                    file,
                    rules,
                    attempt,
                    APPLY_MAX_ATTEMPTS,
                )
        return None, "file_changed_since_review"

    # -- steps ------------------------------------------------------------

    async def _analyze(
        self,
        scm: ScmConnector,
        pr: PullRequest,
        diff: Diff,
        config: EffectiveConfig,
        standards_bundle: standards.StandardsBundle,
    ) -> tuple[list[Finding], list[str], list[str], list[str]]:
        quality = get_quality_connector(config.quality, config=config)
        try:
            known_issues = await quality.fetch_issues(pr, diff)
        except Exception as exc:
            logger.warning("Quality connector %s failed", config.quality, exc_info=True)
            known_issues = []
            quality_warning = [f"quality connector {config.quality} failed ({exc})"]
        else:
            quality_warning = []
        finally:
            await quality.aclose()

        chunks, skipped = build_chunks(diff, config, standards_bundle.catalog)
        context = ReviewContext(
            pr=pr,
            config=config,
            standards=standards_bundle,
            known_issues=known_issues,
            warnings=list(quality_warning),
        )

        if not chunks:
            return list(known_issues), [], skipped, context.warnings

        reviewer = get_reviewer(config.reviewer, config)
        try:
            model_findings = await reviewer.review(chunks, context)
        finally:
            await reviewer.aclose()

        # A review that reached nothing must never report a clean pass — that
        # would turn a model outage into a green light on every pull request.
        if context.failed_chunks >= len(chunks):
            raise ReviewerError(
                f"The reviewer could not review any of the {len(chunks)} chunk(s)",
                detail="; ".join(context.warnings[:3]) or None,
            )

        merged = _merge(known_issues, model_findings)
        return merged, [chunk.path for chunk in chunks], skipped, context.warnings


def _merge(known: list[Finding], produced: list[Finding]) -> list[Finding]:
    """Quality-tool findings win; model findings that repeat them are dropped."""
    merged: list[Finding] = []
    seen: set[tuple[str, int | None, str]] = set()
    for finding in [*known, *produced]:
        key = finding.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        merged.append(finding)
    merged.sort(key=lambda f: (f.file, f.line if f.line is not None else -1, -f.severity.rank))
    return merged


def _apply_refusal(config: EffectiveConfig, pr: PullRequest, requester: str) -> str | None:
    """The request-level gates every apply shares, or None when it may go on."""
    if not config.allow_apply_fixes:
        return "apply_fixes_disabled"
    if requester != pr.author:
        return "unauthorized_requester"
    if pr.is_fork:
        return "fork_pull_request_unsupported"
    return None


def _bot_finding(
    config: EffectiveConfig, author: str, body: str, file: str, line: int | None
) -> Finding | None:
    """Rebuild the `Finding` behind one of the bot's own comments, or None.

    The rendered format is public, so anyone can paste a lookalike comment;
    only the identity that posts reviews is trusted.
    """
    if author not in config.apply_trusted_authors:
        return None
    parsed = parse_finding_comment(body)
    if parsed is None:
        return None
    return Finding(
        file=file,
        line=line,
        severity=Severity(parsed["severity"]),
        rule_id=parsed["rule_id"],
        message=parsed["message"],
        suggestion=parsed["suggestion"],
    )


def _applied_marker(comment_id: str) -> str:
    return f"prreview:applied:{comment_id}"


def _already_applied(comments: list[ExistingComment], comment_id: str) -> bool:
    """A single /apply confirmation carries one marker; an `/apply all`
    summary carries one per suggestion in its body, so check both."""
    marker = _applied_marker(comment_id)
    return any(c.marker == marker or f"<!-- {marker} -->" in c.body for c in comments)


def _bulk_confirmation(
    applied: list[tuple[ExistingComment, Finding, str]], skipped: list[SkippedSuggestion]
) -> CommentDraft:
    commits = list(dict.fromkeys(sha for _, _, sha in applied))
    lines = [
        f"✅ Applied {len(applied)} suggestion(s) in {len(commits)} commit(s):",
        "",
    ]
    for _, finding, sha in applied:
        where = f"{finding.file}:{finding.line}" if finding.line else finding.file
        lines.append(f"- `{finding.rule_id}` on `{where}` → `{sha[:12]}`")
    if skipped:
        lines += ["", f"Not applied ({len(skipped)}):", ""]
        lines += [f"- comment {s.comment_id}: {s.reason}" for s in skipped]
    lines.append("")
    lines += [f"<!-- {_applied_marker(c.comment_id)} -->" for c, _, _ in applied]
    return CommentDraft(body="\n".join(lines), marker=_applied_marker(applied[0][0].comment_id))


def _match_line_endings(original: str, new: str) -> str:
    """Give the model's output the original file's newline style and final
    newline. Models answer in plain `\n`; pushing that into a CRLF file turns a
    one-line fix into a whole-file rewrite, which also outdates every other
    review comment on the file."""
    new = new.replace("\r\n", "\n")
    if original.endswith("\n"):
        if not new.endswith("\n"):
            new += "\n"
    else:
        new = new.rstrip("\n")
    if "\r\n" in original:
        new = new.replace("\n", "\r\n")
    return new


async def _reply_quietly(scm: ScmConnector, pr: PullRequest, comment_id: str, body: str) -> None:
    """Best effort: a reply that fails to post must not change the outcome."""
    try:
        await scm.reply_to_comment(pr, comment_id, body)
    except Exception:
        logger.warning("Could not reply on comment %s", comment_id, exc_info=True)


def _redact_trace_inputs(
    ref: PullRequestRef, overrides: dict[str, Any] | None, **extra: Any
) -> None:
    """Replace the span's auto-captured arguments with token-free ones.

    `@mlflow.trace` records every argument it was called with, and `scm_token`
    is one of them — so without this the caller's PAT is written to the
    experiment in plaintext on every single review, which is exactly the
    guarantee `app/api/auth.py` makes that it never is. Inputs are overwritten
    here, before the span ends and is exported. `extra` carries whatever
    non-secret arguments the specific entry point (`run` vs `apply`) was
    called with.
    """
    try:
        span = mlflow.get_current_active_span()
        if span is None:
            return
        span.set_inputs(
            {
                "ref": ref.model_dump(mode="json"),
                "overrides": overrides or {},
                **extra,
                "scm_token": "(redacted)",
            }
        )
    except Exception:  # tracing must never break a review
        logger.debug("Could not redact the trace inputs", exc_info=True)


def _tag_trace(pr: PullRequest, config: EffectiveConfig) -> None:
    """Make a trace findable from the pull request it reviewed."""
    try:
        mlflow.update_current_trace(
            metadata={
                "pr.slug": pr.ref.slug(),
                # The admin console groups and filters every metric by this.
                "pr.repo": RepoKey.from_ref(pr.ref).path,
                "pr.number": str(pr.ref.pull_request_id),
                "pr.source_commit": pr.source_commit,
                "pr.scm": pr.ref.scm,
                "config.fingerprint": config.fingerprint(),
                "config.model_endpoint": config.model_endpoint,
            }
        )
    except Exception:  # tracing must never break a review
        logger.debug("Could not tag the current trace", exc_info=True)


def _tag_outcome(values: dict[str, Any]) -> None:
    """Record how the traced operation ended, as tags the console can read
    without loading spans. Tags (unlike metadata) can be set at the end."""
    try:
        mlflow.update_current_trace(
            tags={key: "" if value is None else str(value) for key, value in values.items()}
        )
    except Exception:  # tracing must never break a review
        logger.debug("Could not tag the trace outcome", exc_info=True)


def _current_trace_id() -> str | None:
    try:
        span = mlflow.get_current_active_span()
        return span.trace_id if span else None
    except Exception:
        return None
