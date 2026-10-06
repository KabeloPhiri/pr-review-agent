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
    CommentDraft,
    Diff,
    Finding,
    PullRequest,
    PullRequestRef,
    ReviewResult,
    Severity,
)
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

            if not config.allow_apply_fixes:
                return ApplyResult(ref=ref, applied=False, reason="apply_fixes_disabled")
            if requester != pr.author:
                return ApplyResult(ref=ref, applied=False, reason="unauthorized_requester")
            if pr.is_fork:
                return ApplyResult(ref=ref, applied=False, reason="fork_pull_request_unsupported")

            source = await scm.get_comment(pr, comment_id)
            if source is None or source.file is None:
                return ApplyResult(
                    ref=ref, applied=False, reason="comment_not_found_or_unsupported"
                )
            # The rendered format is public, so anyone can paste a lookalike
            # comment; only the identity that posts reviews is trusted.
            if source.author not in config.apply_trusted_authors:
                return ApplyResult(ref=ref, applied=False, reason="not_a_bot_suggestion")

            parsed = parse_finding_comment(source.body)
            if parsed is None:
                return ApplyResult(ref=ref, applied=False, reason="not_a_bot_suggestion")

            # Keyed by the accepted comment, so a second `/apply` or a
            # redelivered webhook cannot apply the same fix twice.
            applied_marker = f"prreview:applied:{comment_id}"
            if any(c.marker == applied_marker for c in await scm.list_comments(pr)):
                return ApplyResult(ref=ref, applied=False, reason="already_applied")

            finding = Finding(
                file=source.file,
                line=source.line,
                severity=Severity(parsed["severity"]),
                rule_id=parsed["rule_id"],
                message=parsed["message"],
                suggestion=parsed["suggestion"],
            )

            applier = get_applier(config.applier, config)
            try:
                commit_sha, reason = await self._push_fix(scm, ref, pr, applier, finding)
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

    async def _push_fix(
        self,
        scm: ScmConnector,
        ref: PullRequestRef,
        pr: PullRequest,
        applier: Any,
        finding: Finding,
    ) -> tuple[str, None] | tuple[None, str]:
        """Generate and push the fix: `(commit_sha, None)` or `(None, reason)`.

        A push is refused (`ScmConflictError`) when the file changed after
        `pr` was read — usually another `/apply` on the same file. Pushing
        the stale patch would revert that change, so re-read the branch and
        regenerate from the new content instead; several `/apply` replies in
        quick succession then land one after another rather than all but
        one being refused.
        """
        for attempt in range(1, APPLY_MAX_ATTEMPTS + 1):
            if attempt > 1:
                pr = await scm.get_pull_request(ref)
            current_text = await scm.get_file_text(pr, finding.file)
            if current_text is None:
                raise ScmError(f"Could not read the current content of {finding.file}")

            new_content = await applier.generate_patch(
                file=finding.file, current_text=current_text, finding=finding
            )
            if new_content:
                new_content = _match_line_endings(current_text, new_content)
            if not new_content or new_content == current_text:
                return None, "no_change_generated"

            try:
                commit_sha = await scm.update_file(
                    pr,
                    finding.file,
                    new_content,
                    message=f"Apply AI review suggestion ({finding.rule_id}) on {finding.file}",
                )
                return commit_sha, None
            except ScmConflictError:
                logger.info(
                    "%s changed while applying %s (attempt %d/%d)",
                    finding.file,
                    finding.rule_id,
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
                "pr.source_commit": pr.source_commit,
                "pr.scm": pr.ref.scm,
                "config.fingerprint": config.fingerprint(),
                "config.model_endpoint": config.model_endpoint,
            }
        )
    except Exception:  # tracing must never break a review
        logger.debug("Could not tag the current trace", exc_info=True)


def _current_trace_id() -> str | None:
    try:
        span = mlflow.get_current_active_span()
        return span.trace_id if span else None
    except Exception:
        return None
