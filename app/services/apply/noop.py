"""The default applier: never produces a change.

Keeping this explicit (rather than allowing `applier: null`) means
`allow_apply_fixes` alone can never push code — a repo has to opt into both
flags, the same defense-in-depth precedent as `quality: noop`
(`app/services/quality/noop.py`).
"""

from __future__ import annotations

from app.core.models import Finding
from app.services.apply.base import Applier, applier_registry


@applier_registry.register("noop")
class NoopApplier(Applier):
    name = "noop"

    async def generate_patch(self, *, file: str, current_text: str, finding: Finding) -> str | None:
        return None
