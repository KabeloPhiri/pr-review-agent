"""Who may use the admin console.

Databricks Apps authenticates every browser request before it reaches the
app and passes the signed-in identity in `X-Forwarded-Email`. Access is two
allowlists set in `databricks.yml` — deliberately not editable from the
console, so nobody can grant themselves access:

    PRREVIEW_CONSOLE_ADMINS   can view and (from the Control release) edit
    PRREVIEW_CONSOLE_VIEWERS  can view

Everyone else gets 403, including the GitHub Actions service principal,
which has `CAN_USE` on the app only to call the review API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from fastapi import Request

ADMINS_ENV = "PRREVIEW_CONSOLE_ADMINS"
VIEWERS_ENV = "PRREVIEW_CONSOLE_VIEWERS"
IDENTITY_HEADER = "x-forwarded-email"


class Role(str, Enum):
    ADMIN = "admin"
    VIEWER = "viewer"


@dataclass(frozen=True)
class ConsoleUser:
    email: str
    role: Role

    @property
    def can_edit(self) -> bool:
        return self.role is Role.ADMIN


class ConsoleAccessDenied(Exception):
    def __init__(self, email: str | None) -> None:
        super().__init__(email or "anonymous")
        self.email = email


def _allowlist(name: str) -> set[str]:
    return {e.strip().lower() for e in os.getenv(name, "").split(",") if e.strip()}


def current_user(request: Request) -> ConsoleUser:
    """The signed-in console user, or `ConsoleAccessDenied`."""
    email = (request.headers.get(IDENTITY_HEADER) or "").strip().lower()
    if email and email in _allowlist(ADMINS_ENV):
        return ConsoleUser(email=email, role=Role.ADMIN)
    if email and email in _allowlist(VIEWERS_ENV):
        return ConsoleUser(email=email, role=Role.VIEWER)
    raise ConsoleAccessDenied(email or None)
