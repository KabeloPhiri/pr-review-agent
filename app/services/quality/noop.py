"""The default quality connector: contributes nothing.

Keeping this explicit (rather than allowing `quality: null`) means the
pipeline always has a connector to call, so wiring a real one later changes
configuration only.
"""

from __future__ import annotations

from app.core.models import Diff, Finding, PullRequest
from app.services.quality.base import QualityConnector, quality_registry


@quality_registry.register("noop")
class NoopQualityConnector(QualityConnector):
    name = "noop"

    async def fetch_issues(self, pr: PullRequest, diff: Diff) -> list[Finding]:
        return []
