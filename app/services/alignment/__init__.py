"""Alignment checker plugins: does the pull request match its requirement?"""

from __future__ import annotations

import importlib

from app.core.errors import ConfigError
from app.services.alignment.base import AlignmentChecker, AlignmentInput, alignment_registry

_MODULES = {
    "llm": "app.services.alignment.llm_checker",
}


def get_alignment_checker(name: str, config, **options) -> AlignmentChecker:
    if name not in alignment_registry and name in _MODULES:
        importlib.import_module(_MODULES[name])
    if name not in alignment_registry:
        raise ConfigError(
            f"Unknown alignment checker: {name!r}",
            detail="Available: " + ", ".join(known_alignment_checkers()),
        )
    return alignment_registry.create(name, config, **options)


def known_alignment_checkers() -> list[str]:
    return sorted(set(_MODULES) | set(alignment_registry.available()))


__all__ = [
    "AlignmentChecker",
    "AlignmentInput",
    "alignment_registry",
    "get_alignment_checker",
    "known_alignment_checkers",
]
