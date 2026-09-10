"""Credential handling for outbound SCM calls.

The pipeline that triggers a review supplies its own SCM token (an Azure
DevOps PAT from a pipeline library variable). Nothing is stored: the token
lives on the request, is passed to the connector, and is held only for the
lifetime of the review — including for background jobs, which keep it in
memory and never persist or log it.

Inbound authentication is separate and handled by the platform: a deployed
Databricks App only accepts Databricks OAuth bearer tokens.
"""

from __future__ import annotations

from fastapi import Request

#: Checked in order. `X-ADO-Token` is accepted because it reads naturally in
#: an Azure DevOps pipeline definition.
TOKEN_HEADERS = ("x-scm-token", "x-ado-token", "x-azure-devops-token")


def token_from_request(request: Request, body_token: str | None = None) -> str | None:
    for header in TOKEN_HEADERS:
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()
    return body_token.strip() if body_token and body_token.strip() else None


def redact(value: str | None, keep: int = 4) -> str:
    """Render a token safely if it ever needs to appear in a log line."""
    if not value:
        return "(none)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"
