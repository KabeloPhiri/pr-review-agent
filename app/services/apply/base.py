"""Applier contract.

An applier turns one accepted `Finding` into the corrected full content of
the file it was raised against. It is deliberately single-file,
single-finding — the same granularity a `/apply` reply works at — not a
bulk operation like `Reviewer.review`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.config import EffectiveConfig
from app.core.models import Finding
from app.core.registry import Registry


class Applier(ABC):
    name: str = "applier"

    def __init__(self, config: EffectiveConfig, **options) -> None:
        self.config = config
        self.options = options

    @abstractmethod
    async def generate_patch(self, *, file: str, current_text: str, finding: Finding) -> str | None:
        """Return the corrected full file content, or `None` if no safe change
        could be produced. `None` is a soft outcome, not a failure."""

    async def aclose(self) -> None:
        return None


applier_registry: Registry[Applier] = Registry("applier")
