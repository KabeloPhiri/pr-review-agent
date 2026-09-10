"""Post findings to the pull request, idempotently.

The pull request itself is the state store: existing comments are read back,
their markers tell us what this agent already said, and only the difference is
posted. That is what makes re-running the pipeline on every push safe.
"""

from __future__ import annotations

import logging

from app.core.config import EffectiveConfig
from app.core.models import PullRequest, ReviewResult
from app.services.publish.formatter import (
    MARKER_PREFIX,
    SUMMARY_RULE,
    marker_for,
    render_finding,
    render_summary,
)
from app.services.scm.base import ScmConnector

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, scm: ScmConnector, config: EffectiveConfig) -> None:
        self.scm = scm
        self.config = config

    async def publish(self, pr: PullRequest, result: ReviewResult) -> int:
        """Post new comments, close stale ones, set the status. Returns posts."""
        if not self.config.post_comments:
            logger.info("post_comments disabled; skipping %d finding(s)", len(result.findings))
            await self._set_status(pr, result)
            return 0

        existing = await self.scm.list_comments(pr)
        existing_markers = {c.marker for c in existing if c.marker}

        posted = 0
        current_markers: set[str] = set()
        for finding in result.findings:
            if finding.line is None:
                continue  # carried in the summary comment instead
            marker = marker_for(finding)
            current_markers.add(marker)
            if marker in existing_markers:
                continue
            try:
                await self.scm.post_comment(pr, render_finding(finding))
                posted += 1
            except Exception as exc:
                logger.warning(
                    "Could not comment on %s:%s", finding.file, finding.line, exc_info=True
                )
                result.warnings.append(f"comment failed for {finding.file}:{finding.line} ({exc})")

        summary = render_summary(result)
        if summary.marker not in existing_markers:
            try:
                await self.scm.post_comment(pr, summary)
                posted += 1
            except Exception as exc:
                logger.warning("Could not post the summary comment", exc_info=True)
                result.warnings.append(f"summary comment failed ({exc})")

        await self._close_resolved(pr, existing, current_markers)
        await self._set_status(pr, result)

        result.comments_posted = posted
        result.published = True
        return posted

    async def _close_resolved(self, pr: PullRequest, existing, current_markers: set[str]) -> None:
        """Resolve threads whose finding is gone from the latest review."""
        for comment in existing:
            marker = comment.marker or ""
            if not marker.startswith(f"{MARKER_PREFIX}:") or comment.is_closed:
                continue
            if marker.startswith(f"{MARKER_PREFIX}:{SUMMARY_RULE}:") or marker in current_markers:
                continue
            try:
                await self.scm.close_comment(pr, comment.thread_id)
            except Exception:
                logger.warning("Could not close thread %s", comment.thread_id, exc_info=True)

    async def _set_status(self, pr: PullRequest, result: ReviewResult) -> None:
        if not self.config.post_status:
            return
        counts = result.counts_by_severity()
        description = (
            f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"
            if result.findings
            else "No findings"
        )
        try:
            await self.scm.set_status(
                pr,
                result.verdict,
                description,
                name=self.config.status_name,
                genre=self.config.status_genre,
            )
        except Exception as exc:
            logger.warning("Could not set the pull request status", exc_info=True)
            result.warnings.append(f"status update failed ({exc})")
