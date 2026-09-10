"""Reviewer contract.

A reviewer turns chunks into findings. The default implementation is a single
structured-output call per chunk; a tool-using agent (LangGraph, Agents SDK)
can replace it without the pipeline noticing, because this is the only
surface it is called through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.config import EffectiveConfig
from app.core.models import Finding, PullRequest
from app.core.registry import Registry
from app.services.policy.standards import StandardsBundle
from app.services.review.chunking import ReviewChunk


@dataclass
class ReviewContext:
    pr: PullRequest
    config: EffectiveConfig
    standards: StandardsBundle
    known_issues: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def known_issues_for(self, path: str) -> list[Finding]:
        return [issue for issue in self.known_issues if issue.file == path]


class Reviewer(ABC):
    name: str = "reviewer"

    def __init__(self, config: EffectiveConfig, **options) -> None:
        self.config = config
        self.options = options

    @abstractmethod
    async def review(self, chunks: list[ReviewChunk], context: ReviewContext) -> list[Finding]:
        """Produce findings for every chunk."""

    async def aclose(self) -> None:
        return None


reviewer_registry: Registry[Reviewer] = Registry("reviewer")
