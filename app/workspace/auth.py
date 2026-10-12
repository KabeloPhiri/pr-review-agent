"""Who may open the PR workspace.

Databricks Apps signs every browser in before a request reaches the app and
passes the identity in `X-Forwarded-Email`. Unlike the admin console the
workspace is for every pull request author and reviewer, so by default any
signed-in user may open it; `PRREVIEW_WORKSPACE_USERS` narrows that to an
allowlist. What a user may *do* to a pull request is decided by GitHub — the
token they paste — not by this file.

Local development has no proxy to set the header, so
`PRREVIEW_WORKSPACE_DEV_EMAIL` stands in for it, but only when the process
is not running as a Databricks App (`DATABRICKS_APP_NAME` unset).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Request

USERS_ENV = "PRREVIEW_WORKSPACE_USERS"
DEV_EMAIL_ENV = "PRREVIEW_WORKSPACE_DEV_EMAIL"
IDENTITY_HEADER = "x-forwarded-email"


@dataclass(frozen=True)
class WorkspaceUser:
    email: str


class WorkspaceAccessDenied(Exception):
    def __init__(self, email: str | None) -> None:
        super().__init__(email or "anonymous")
        self.email = email


def is_deployed() -> bool:
    """Running as a Databricks App, where development shortcuts are off."""
    return bool(os.getenv("DATABRICKS_APP_NAME"))


def current_user(request: Request) -> WorkspaceUser:
    """The signed-in workspace user, or `WorkspaceAccessDenied`."""
    email = (request.headers.get(IDENTITY_HEADER) or "").strip().lower()
    if not email and not is_deployed():
        email = os.getenv(DEV_EMAIL_ENV, "").strip().lower()
    if not email:
        raise WorkspaceAccessDenied(None)
    allowed = {e.strip().lower() for e in os.getenv(USERS_ENV, "").split(",") if e.strip()}
    if allowed and email not in allowed:
        raise WorkspaceAccessDenied(email)
    return WorkspaceUser(email=email)
