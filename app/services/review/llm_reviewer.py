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

from app.core.llm_client import build_client, complete, message_text
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
            self._client = build_client()
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
        return complete(
            self.client,
            model=self.config.model_endpoint,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=self.config.temperature,
        )

    # -- response handling ------------------------------------------------

    def _parse(self, content: str, chunk: ReviewChunk, context: ReviewContext) -> list[Finding]:
        """Validate the reply.

        A malformed reply degrades this chunk to a warning rather than raising,
        so one bad reply cannot block a merge. It still counts as a chunk that
        was not reviewed: a reply truncated mid-JSON is indistinguishable from
        no review at all, and if every chunk ends up here the pipeline must
        fail the run rather than report a clean pass.
        """
        cleaned = _FENCE_RE.sub("", content).strip()
        if not cleaned:
            # An empty reply is the model saying "nothing to report", which is
            # a legitimate clean result rather than a failure.
            return []
        try:
            payload = json.loads(cleaned)
            raw = _RawFindings(**payload) if isinstance(payload, dict) else _RawFindings()
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning("Unparseable model reply for %s: %s", chunk.path, exc)
            context.warnings.append(f"{chunk.path}: model reply could not be parsed")
            context.failed_chunks += 1
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
                    model=self.config.model_endpoint,
                )
            )
        return findings


#: Kept as an alias: moved to `app.core.llm_client` so `app/services/apply/`
#: can reuse it without reaching into this plugin's own package.
_message_text = message_text


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
