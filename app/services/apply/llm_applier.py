"""Default applier: one structured-output model call for the whole file.

Deliberately full-file-in/full-file-out rather than a diff: GitHub's Contents
API (`ScmConnector.update_file`) takes full new content anyway, and applying
a model-produced unified diff by hand is far more fragile than asking for the
corrected file directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import ReviewerError
from app.core.llm_client import build_client, complete
from app.core.models import Finding
from app.services.apply.base import Applier, applier_registry
from app.services.apply.prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class _RawPatch(BaseModel):
    model_config = {"extra": "ignore"}

    file_content: str = ""


@applier_registry.register("llm")
class LlmApplier(Applier):
    name = "llm"

    def __init__(self, config, *, client: Any | None = None, **options) -> None:
        super().__init__(config, **options)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = build_client()
        return self._client

    async def generate_patch(self, *, file: str, current_text: str, finding: Finding) -> str | None:
        content = await asyncio.to_thread(
            complete,
            self.client,
            model=self.config.model_endpoint,
            system_prompt=build_system_prompt(),
            user_prompt=build_user_prompt(file, current_text, finding),
            temperature=self.config.temperature,
        )
        return self._parse(content, file)

    def _parse(self, content: str, file: str) -> str | None:
        cleaned = _FENCE_RE.sub("", content).strip()
        if not cleaned:
            return None
        try:
            payload = json.loads(cleaned)
            raw = _RawPatch(**payload) if isinstance(payload, dict) else _RawPatch()
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning("Unparseable apply reply for %s: %s", file, exc)
            raise ReviewerError(
                f"Could not parse the model's patch for {file}", detail=str(exc)
            ) from exc
        return raw.file_content or None
