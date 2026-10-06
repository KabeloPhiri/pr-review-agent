"""Settings managed from the admin console.

The console lets an admin change configuration, standards and prompt
guidance without a redeploy. This module is the only thing the review path
knows about that: a small `SettingsStore` interface, selected by the
`PRREVIEW_SETTINGS_STORE` env var. The default, `none`, changes nothing, so
the app behaves exactly as before until a store is configured — and tests
never need one.

Settings and statistics are both keyed by `RepoKey`, so what the console
shows for a repository and what it configures for that repository line up.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.core.models import PullRequestRef

logger = logging.getLogger(__name__)

STORE_ENV = "PRREVIEW_SETTINGS_STORE"


@dataclass(frozen=True, order=True)
class RepoKey:
    """One repository on one SCM, e.g. `github` + `KabeloPhiri/pr-review-agent`."""

    scm: str
    slug: str

    @classmethod
    def from_ref(cls, ref: PullRequestRef) -> "RepoKey":
        return cls(scm=ref.scm, slug=ref.repo_slug())

    @classmethod
    def parse(cls, value: str) -> "RepoKey":
        """Inverse of `str()`: `github/KabeloPhiri__pr-review-agent`."""
        scm, _, rest = value.partition("/")
        if not scm or not rest:
            raise ValueError(f"Not a repository key: {value!r}")
        return cls(scm=scm, slug=rest.replace("__", "/"))

    @property
    def path(self) -> str:
        """Filesystem-safe form, used for settings paths and as the trace tag."""
        return f"{self.scm}/{self.slug.replace('/', '__')}"

    def __str__(self) -> str:
        return self.path


class SettingsStore(Protocol):
    """What the review path reads from the console's settings."""

    def config_overrides(self, repo: RepoKey | None = None) -> dict[str, Any]:
        """Global console overrides, with `repo`'s own overrides merged on top."""
        ...

    def standards_dir(self) -> Path | None:
        """A local directory of standards replacing the bundled set, or None."""
        ...

    def repo_standards(self, repo: RepoKey) -> str:
        """Extra standards text for one repository ('' when none)."""
        ...

    def prompt(self, name: str) -> str | None:
        """Replacement guidance for a prompt (`review_guidance`, `apply_guidance`)."""
        ...

    def console_settings(self) -> dict[str, Any]:
        """Console-only settings such as model prices; not review config."""
        ...

    def repos(self) -> list[RepoKey]:
        """Repositories that have their own settings."""
        ...


class NullStore:
    """No console settings: every lookup falls through to today's behaviour."""

    def config_overrides(self, repo: RepoKey | None = None) -> dict[str, Any]:
        return {}

    def standards_dir(self) -> Path | None:
        return None

    def repo_standards(self, repo: RepoKey) -> str:
        return ""

    def prompt(self, name: str) -> str | None:
        return None

    def console_settings(self) -> dict[str, Any]:
        return {}

    def repos(self) -> list[RepoKey]:
        return []


_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    """The process-wide store, chosen once from `PRREVIEW_SETTINGS_STORE`."""
    global _store
    if _store is None:
        kind = os.getenv(STORE_ENV, "none").strip().lower()
        if kind != "none":
            logger.warning("Unknown %s=%r; using no console settings", STORE_ENV, kind)
        _store = NullStore()
    return _store


def set_settings_store(store: SettingsStore | None) -> None:
    """Swap the store (tests, and the console after it is configured)."""
    global _store
    _store = store
