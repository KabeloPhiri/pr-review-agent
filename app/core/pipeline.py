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
from app.core.errors import ReviewerError
from app.core.models import Diff, Finding, PullRequest, PullRequestRef, ReviewResult
from app.services.policy import gate, standards
from app.services.publish.publisher import Publisher
from app.services.quality import get_quality_connector
from app.services.review import ReviewContext, get_reviewer
from app.services.review.chunking import build_chunks
from app.services.scm import ScmConnector, get_connector

logger = logging.getLogger(__name__)


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
        _redact_trace_inputs(ref, overrides, publish)
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


def _redact_trace_inputs(
    ref: PullRequestRef, overrides: dict[str, Any] | None, publish: bool
) -> None:
    """Replace the span's auto-captured arguments with token-free ones.

    `@mlflow.trace` records every argument it was called with, and `scm_token`
    is one of them — so without this the caller's PAT is written to the
    experiment in plaintext on every single review, which is exactly the
    guarantee `app/api/auth.py` makes that it never is. Inputs are overwritten
    here, before the span ends and is exported.
    """
    try:
        span = mlflow.get_current_active_span()
        if span is None:
            return
        span.set_inputs(
            {
                "ref": ref.model_dump(mode="json"),
                "overrides": overrides or {},
                "publish": publish,
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
