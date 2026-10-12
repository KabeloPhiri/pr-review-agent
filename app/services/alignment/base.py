"""Alignment checker contract.

A checker answers one question about a whole pull request: does the diff do
what the requester says it is for? Unlike a reviewer it sees every changed
file at once (a requirement is met or missed across files, not per file) and
produces one verdict rather than line findings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.config import EffectiveConfig
from app.core.models import AlignmentResult, ChangeIntent, PullRequest
from app.core.registry import Registry


@dataclass
class AlignmentInput:
    pr: PullRequest
    intent: ChangeIntent
    #: The requirement or acceptance criteria, as the requester pasted it.
    requirement: str
    #: Every changed file, rendered with new-file line numbers.
    diff_text: str
    files: list[str] = field(default_factory=list)
    #: Changed files left out of `diff_text` (binary, excluded, over budget).
    omitted: list[str] = field(default_factory=list)


class AlignmentChecker(ABC):
    name: str = "alignment"

    def __init__(self, config: EffectiveConfig, **options) -> None:
        self.config = config
        self.options = options

    @abstractmethod
    async def check(self, request: AlignmentInput) -> AlignmentResult:
        """Judge the diff against the requirement.

        Raise `ReviewerError` when no judgement could be made (model outage,
        unparseable reply). Never fall back to a verdict: a check that read
        nothing must not report `aligned`.
        """

    async def aclose(self) -> None:
        return None


alignment_registry: Registry[AlignmentChecker] = Registry("alignment checker")
