"""Shared Databricks-hosted OpenAI-compatible client plumbing.

Lives in `core` rather than under `app/services/review/` or
`app/services/apply/` because both of those plugin families use it, and
`CLAUDE.md`'s architecture rule is "nothing outside a plugin's own package
imports it" — a cross-cutting helper belongs here, not inside either one.
"""

from __future__ import annotations

import logging
from typing import Any

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
    """One chat-completion call, with the response-format fallback every
    endpoint needs: not every serving endpoint supports `response_format`,
    and the prompt already demands bare JSON, so retry without it before
    failing outright.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    try:
        response = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception as exc:
        logger.info("Retrying without response_format: %s", exc)
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as inner:
            raise ReviewerError(
                f"Model endpoint {model!r} call failed", detail=str(inner)
            ) from inner
    return message_text(response.choices[0].message)


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
