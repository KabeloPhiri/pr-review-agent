"""Default alignment checker: one structured-output model call per pull request."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import ReviewerError
from app.core.llm_client import build_client, complete
from app.core.models import (
    AlignmentCriterion,
    AlignmentResult,
    AlignmentVerdict,
    ChangeIntent,
    CriterionStatus,
    UnrelatedChange,
)
from app.services.alignment.base import AlignmentChecker, AlignmentInput, alignment_registry
from app.services.alignment.prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class _RawAssessment(BaseModel):
    model_config = {"extra": "ignore"}

    verdict: AlignmentVerdict
    summary: str = ""
    criteria: list[dict[str, Any]] = []
    unrelated_changes: list[dict[str, Any]] = []
    intent_matches: bool = True
    detected_intent: str | None = None


@alignment_registry.register("llm")
class LlmAlignmentChecker(AlignmentChecker):
    name = "llm"

    def __init__(self, config, *, client: Any | None = None, **options) -> None:
        super().__init__(config, **options)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = build_client()
        return self._client

    async def check(self, request: AlignmentInput) -> AlignmentResult:
        content = await asyncio.to_thread(
            complete,
            self.client,
            model=self.config.model_endpoint,
            system_prompt=build_system_prompt(),
            user_prompt=build_user_prompt(request),
            temperature=self.config.temperature,
        )
        return parse_assessment(content, request)


def parse_assessment(content: str, request: AlignmentInput) -> AlignmentResult:
    """The model's reply as an `AlignmentResult`, or `ReviewerError`.

    Unlike a review, an empty reply is a failure: "the code is fine" is a
    valid review, but there is no verdict in silence. A malformed criterion is
    dropped with a warning rather than failing the whole check.
    """
    cleaned = _FENCE_RE.sub("", content or "").strip()
    if not cleaned:
        raise ReviewerError("The model returned an empty alignment assessment")
    try:
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise TypeError("expected a JSON object")
        raw = _RawAssessment(**payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("Unparseable alignment reply for %s: %s", request.pr.ref.slug(), exc)
        raise ReviewerError(
            "Could not parse the model's alignment assessment", detail=str(exc)[:500]
        ) from exc

    warnings: list[str] = []
    criteria: list[AlignmentCriterion] = []
    for item in raw.criteria:
        try:
            criteria.append(
                AlignmentCriterion(
                    text=str(item.get("text", "")).strip(),
                    status=CriterionStatus(str(item.get("status", "")).lower()),
                    evidence=[str(e) for e in item.get("evidence") or [] if e],
                    note=str(item.get("note") or ""),
                )
            )
        except (ValueError, ValidationError, AttributeError):
            warnings.append("dropped a criterion the model returned in an unknown shape")
    unrelated = [
        UnrelatedChange(file=str(item["file"]), reason=str(item.get("reason") or ""))
        for item in raw.unrelated_changes
        if isinstance(item, dict) and item.get("file")
    ]
    try:
        detected = ChangeIntent(raw.detected_intent) if raw.detected_intent else None
    except ValueError:
        detected = None

    return AlignmentResult(
        ref=request.pr.ref,
        intent=request.intent,
        verdict=raw.verdict,
        summary=raw.summary.strip(),
        criteria=[c for c in criteria if c.text],
        unrelated_changes=unrelated,
        intent_matches=raw.intent_matches,
        detected_intent=detected,
        files_considered=len(request.files),
        warnings=warnings,
    )
