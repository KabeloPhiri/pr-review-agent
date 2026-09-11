"""MLflow agent handler.

`POST /invocations` runs one synchronous review from a plain JSON body. It
exists so the app is a first-class MLflow agent — traceable, smoke-testable
with the standard tooling, and evaluable — while the REST routes in
`app.api.routes` carry the real pipeline-facing API (async jobs, polling,
config inspection).
"""

from __future__ import annotations

import logging

from mlflow.genai.agent_server import invoke

from app.api.auth import TOKEN_HEADERS
from app.api.schemas import ReviewRequest
from app.core.pipeline import ReviewPipeline

logger = logging.getLogger(__name__)


@invoke()
async def invoke_handler(data: dict) -> dict:
    """Review one pull request and return the result.

    The payload is a `ReviewRequest`; `mode` is ignored because this endpoint
    is always synchronous.
    """
    body = ReviewRequest(**data)
    _redact_token_from_trace(data)
    result = await ReviewPipeline().run(
        body.to_ref(),
        scm_token=_token_from_headers() or body.scm_token,
        overrides=body.config,
        publish=body.publish,
    )
    return result.model_dump(mode="json")


def _redact_token_from_trace(data: dict) -> None:
    """Keep a body-supplied SCM token out of this request's span.

    The agent server records the raw request body as the span inputs before
    this handler runs, so a caller who sends `scm_token` in the body would
    have it written to the experiment verbatim. Overwriting the inputs here
    is what keeps the "never stored" guarantee true for `/invocations` too.
    """
    if not data.get("scm_token"):
        return
    try:
        import mlflow

        span = mlflow.get_current_active_span()
        if span is not None:
            span.set_inputs({**data, "scm_token": "(redacted)"})
    except Exception:  # tracing must never break a review
        logger.debug("Could not redact the SCM token from the trace", exc_info=True)


def _token_from_headers() -> str | None:
    """Read the SCM token from the request headers, if the server exposes them."""
    try:
        from mlflow.genai.agent_server import get_request_headers

        headers = get_request_headers() or {}
    except Exception:
        return None
    lowered = {key.lower(): value for key, value in headers.items()}
    for header in TOKEN_HEADERS:
        value = lowered.get(header)
        if value and value.strip():
            return value.strip()
    return None
