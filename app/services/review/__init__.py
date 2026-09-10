"""Reviewer plugins."""

from __future__ import annotations

import importlib

from app.core.errors import ConfigError
from app.services.review.base import ReviewContext, Reviewer, reviewer_registry

_MODULES = {
    "llm": "app.services.review.llm_reviewer",
}


def get_reviewer(name: str, config, **options) -> Reviewer:
    if name not in reviewer_registry and name in _MODULES:
        importlib.import_module(_MODULES[name])
    if name not in reviewer_registry:
        raise ConfigError(
            f"Unknown reviewer: {name!r}",
            detail="Available: " + ", ".join(known_reviewers()),
        )
    return reviewer_registry.create(name, config, **options)


def known_reviewers() -> list[str]:
    return sorted(set(_MODULES) | set(reviewer_registry.available()))


__all__ = [
    "ReviewContext",
    "Reviewer",
    "get_reviewer",
    "known_reviewers",
    "reviewer_registry",
]
