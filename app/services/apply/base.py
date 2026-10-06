"""Applier contract.

An applier turns accepted `Finding`s into the corrected full content of the
file they were raised against — always one file at a time. `generate_patch`
handles one finding (a `/apply` reply); `generate_patch_many` handles every
accepted finding in a file at once (`/apply all`).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.core.config import EffectiveConfig
from app.core.models import Finding
from app.core.registry import Registry

logger = logging.getLogger(__name__)


class Applier(ABC):
    name: str = "applier"

    def __init__(self, config: EffectiveConfig, **options) -> None:
        self.config = config
        self.options = options

    @abstractmethod
    async def generate_patch(self, *, file: str, current_text: str, finding: Finding) -> str | None:
        """Return the corrected full file content, or `None` if no safe change
        could be produced. `None` is a soft outcome, not a failure."""

    async def generate_patch_many(
        self, *, file: str, current_text: str, findings: list[Finding]
    ) -> str | None:
        """As many of `findings` as could be applied to one file.

        Findings for which no safe change could be produced are skipped and
        logged.

        The default chains `generate_patch`, each call on the previous
        result, so any applier supports `/apply all`; appliers that can do it
        in one call (`llm`) override this.
        """
        text = current_text
        for index, finding in enumerate(findings):
            patched = await self.generate_patch(file=file, current_text=text, finding=finding)
            if patched is not None:
                text = patched
            else:
                logger.info(
                    "Skipped finding %d of %d for %s: no safe change produced",
                    index + 1,
                    len(findings),
                    file,
                )
        return text if text != current_text else None

    async def aclose(self) -> None:
        return None


applier_registry: Registry[Applier] = Registry("applier")
