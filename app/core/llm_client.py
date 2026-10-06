"""Shared Databricks-hosted OpenAI-compatible client plumbing.

Lives in `core` rather than under `app/services/review/` or
`app/services/apply/` because both of those plugin families use it, and
`CLAUDE.md`'s architecture rule is "nothing outside a plugin's own package
imports it" — a cross-cutting helper belongs here, not inside either one.
"""

from __future__ import annotations

import logging
from typing import Any

import mlflow
from mlflow.entities import SpanType
from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey

from app.core.errors import ReviewerError

logger = logging.getLogger(__name__)


def build_client() -> Any:
    """Build the lazily-constructed Databricks serving client.

    Raises `ReviewerError` rather than importing `databricks.sdk` at module
    load time, so running the tests never requires Databricks credentials.
    """
    try:
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient().serving_endpoints.get_open_ai_client()
    except Exception as exc:  # pragma: no cover - credential/environment specific
        raise ReviewerError(
            "Could not create the Databricks serving client",
            detail=str(exc),
        ) from exc


def complete(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
) -> str:
    """One chat-completion call, dropping optional parameters the endpoint
    rejects. Not every serving endpoint supports `response_format`, and newer
    Anthropic models on Databricks also reject `temperature` (400 "does not
    support the temperature parameter"). The prompt already demands bare
    JSON, so each optional parameter is retried away before failing outright.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    optional: dict[str, Any] = {
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    # One span per model call, carrying its token usage. MLflow totals these
    # into the trace's `token_usage`, which the admin console reports per
    # model and per repository. `asyncio.to_thread` copies context, so the
    # span nests under the review's trace even though this runs in a thread.
    with mlflow.start_span(name=f"llm:{model}", span_type=SpanType.CHAT_MODEL) as span:
        span.set_attribute("model", model)
        response = _create(client, model, messages, optional)
        _record_usage(span, response)
    return message_text(response.choices[0].message)


def _create(client: Any, model: str, messages: list, optional: dict[str, Any]) -> Any:
    """The call itself, retrying away optional parameters the endpoint rejects."""
    while True:
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, **optional
            )
            break
        except Exception as exc:
            dropped = _rejected_parameter(exc, optional)
            if dropped is None:
                raise ReviewerError(
                    f"Model endpoint {model!r} call failed", detail=str(exc)
                ) from exc
            logger.info("Retrying %s without %s: %s", model, dropped, exc)
            optional.pop(dropped)
    return response


def _record_usage(span: Any, response: Any) -> None:
    """Copy the OpenAI-style `usage` block onto the span, if there is one."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0) or input_tokens + output_tokens
    span.set_attribute(
        SpanAttributeKey.CHAT_USAGE,
        {
            TokenUsageKey.INPUT_TOKENS: input_tokens,
            TokenUsageKey.OUTPUT_TOKENS: output_tokens,
            TokenUsageKey.TOTAL_TOKENS: total,
        },
    )


def _rejected_parameter(exc: Exception, optional: dict[str, Any]) -> str | None:
    """Which optional parameter to drop after `exc`, or None to give up.

    `temperature` only when the error names it, since a deterministic review
    is worth keeping wherever the endpoint allows it. `response_format` on any
    other error, as before: endpoints word that rejection too many ways.
    """
    if "temperature" in optional and "temperature" in str(exc).lower():
        return "temperature"
    if "response_format" in optional:
        return "response_format"
    return None


def message_text(message: Any) -> str:
    """Pull the assistant's text out of a reply, whatever shape it arrives in.

    Most endpoints set `content` to a plain string. Reasoning models served on
    Databricks (the `gpt-oss` family, for one) instead return a list of parts —
    `{"type": "reasoning", ...}` followed by `{"type": "text", "text": ...}` —
    and only the text part is the answer. Concatenating everything would feed
    the model's own chain of thought to the JSON parser.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            get = (
                part.get
                if isinstance(part, dict)
                else lambda key, _d=None, _p=part: getattr(_p, key, _d)
            )
            if get("type") == "text":
                parts.append(str(get("text") or ""))
        if parts:
            return "\n".join(parts)
    # Some gateways only populate the streaming-style field.
    return str(getattr(message, "reasoning_content", "") or "")
