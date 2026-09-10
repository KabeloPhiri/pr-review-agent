"""Quality-tool connector contract.

A quality connector brings findings that something *other* than the model
already produced — SonarQube, a linter, a security scanner. They are merged
into the review so the model can be told "this is already reported, do not
repeat it", and so the gate sees one combined list.

v1 ships the contract plus a no-op; the SonarQube adapter is a new module
here and one line in `_MODULES`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import Diff, Finding, PullRequest
from app.core.registry import Registry


class QualityConnector(ABC):
    name: str = "quality"

    def __init__(self, config=None, **options) -> None:
        self.config = config
        self.options = options

    @abstractmethod
    async def fetch_issues(self, pr: PullRequest, diff: Diff) -> list[Finding]:
        """Return findings for this pull request, already scoped to the diff."""

    async def aclose(self) -> None:
        return None


quality_registry: Registry[QualityConnector] = Registry("quality connector")
