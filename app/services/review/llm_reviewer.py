"""Default reviewer: one structured-output model call per chunk.

Deliberately not an agent loop. A single call per chunk with temperature 0 is
reproducible, cheap, easy to trace, and easy to evaluate — and the `Reviewer`
interface leaves the door open for a tool-using implementation later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.core.errors import ReviewerError
from app.core.models import Finding, Severity
from app.services.review.base import ReviewContext, Reviewer, reviewer_registry
from app.services.review.chunking import ReviewChunk
from app.services.review.prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
DEFAULT_CONCURRENCY = int(os.getenv("PRREVIEW_MODEL_CONCURRENCY", "4"))


class _RawFinding(BaseModel):
    """What we accept from the model before it becomes a `Finding`."""

    model_config = {"extra": "ignore"}

    line: int | None = None
    end_line: int | None = None
    severity: str = "warning"
    rule_id: str = "general"
    message: str
    suggestion: str | None = None


class _RawFindings(BaseModel):
    model_config = {"extra": "ignore"}

    findings: list[_RawFinding] = Field(default_factory=list)


@reviewer_registry.register("llm")
class LlmReviewer(Reviewer):
    name = "llm"

    def __init__(self, config, *, client: Any | None = None, **options) -> None:
        super().__init__(config, **options)
        self._client = client
        self._semaphore = asyncio.Semaphore(options.get("concurrency", DEFAULT_CONCURRENCY))

    # -- client -----------------------------------------------------------

    @property
    def client(self) -> Any:
        """Databricks-hosted OpenAI-compatible client, created on first use.

        Built lazily so importing this module (and running the tests) never
        requires Databricks credentials.
        """
        if self._client is None:
            try:
                from databricks.sdk import WorkspaceClient

                self._client = WorkspaceClient().serving_endpoints.get_open_ai_client()
            except Exception as exc:  # pragma: no cover - credential/environment specific
                raise ReviewerError(
                    "Could not create the Databricks serving client",
                    detail=str(exc),
                ) from exc
        return self._client

    # -- reviewing --------------------------------------------------------

    async def review(self, chunks: list[ReviewChunk], context: ReviewContext) -> list[Finding]:
        results = await asyncio.gather(
            *(self._review_chunk(chunk, context) for chunk in chunks),
            return_exceptions=True,
        )

        findings: list[Finding] = []
        per_file: dict[str, int] = {}
        for chunk, result in zip(chunks, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Review failed for %s: %s", chunk.path, result, exc_info=result)
                context.warnings.append(f"{chunk.path}: review failed ({result})")
                context.failed_chunks += 1
                continue
            for finding in result:
                seen = per_file.get(finding.file, 0)
                if seen >= self.config.max_findings_per_file:
                    continue
                per_file[finding.file] = seen + 1
                findings.append(finding)
        return findings

    async def _review_chunk(self, chunk: ReviewChunk, context: ReviewContext) -> list[Finding]:
        async with self._semaphore:
            content = await asyncio.to_thread(
                self._complete,
                build_system_prompt(chunk, context),
                build_user_prompt(chunk, context),
            )
        return self._parse(content, chunk, context)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.config.model_endpoint,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        try:
            response = self.client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except Exception as exc:
            # Not every serving endpoint supports response_format; the prompt
            # already demands bare JSON, so retry without it before failing.
            logger.info("Retrying without response_format: %s", exc)
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as inner:
                raise ReviewerError(
                    f"Model endpoint {self.config.model_endpoint!r} call failed",
                    detail=str(inner),
                ) from inner
        return response.choices[0].message.content or ""

    # -- response handling ------------------------------------------------

    def _parse(self, content: str, chunk: ReviewChunk, context: ReviewContext) -> list[Finding]:
        """Validate the reply.

        A malformed reply degrades this chunk to a warning on the review
        rather than failing the pull request — a parser bug must never block
        a merge.
        """
        cleaned = _FENCE_RE.sub("", content).strip()
        if not cleaned:
            return []
        try:
            payload = json.loads(cleaned)
            raw = _RawFindings(**payload) if isinstance(payload, dict) else _RawFindings()
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning("Unparseable model reply for %s: %s", chunk.path, exc)
            context.warnings.append(f"{chunk.path}: model reply could not be parsed")
            return []

        findings: list[Finding] = []
        for item in raw.findings:
            if not item.message.strip():
                continue
            line = _snap_to_changed_line(item.line, chunk.changed_lines)
            findings.append(
                Finding(
                    file=chunk.path,
                    line=line,
                    end_line=item.end_line if line is not None else None,
                    severity=_coerce_severity(item.severity),
                    rule_id=item.rule_id.strip() or "general",
                    message=item.message.strip(),
                    suggestion=(item.suggestion or "").strip() or None,
                    source="llm",
                    language=chunk.language,
                )
            )
        return findings


def _coerce_severity(value: str) -> Severity:
    try:
        return Severity(value.strip().lower())
    except ValueError:
        return Severity.WARNING


def _snap_to_changed_line(line: int | None, changed: set[int]) -> int | None:
    """Keep comments on lines this pull request actually touched.

    Models occasionally cite a nearby context line. Snapping to the closest
    changed line within a couple of lines keeps a useful finding anchored;
    anything further away becomes a file-level comment (`line=None`) instead
    of being dropped or posted in the wrong place.
    """
    if line is None or not changed:
        return None
    if line in changed:
        return line
    nearest = min(changed, key=lambda candidate: abs(candidate - line))
    return nearest if abs(nearest - line) <= 2 else None
