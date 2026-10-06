"""Applier plugins: turn an accepted finding into new file content."""

from __future__ import annotations

import importlib

from app.core.errors import ConfigError
from app.services.apply.base import Applier, applier_registry

_MODULES = {
    "llm": "app.services.apply.llm_applier",
    "noop": "app.services.apply.noop",
}


def get_applier(name: str, config, **options) -> Applier:
    if name not in applier_registry and name in _MODULES:
        importlib.import_module(_MODULES[name])
    if name not in applier_registry:
        raise ConfigError(
            f"Unknown applier: {name!r}",
            detail="Available: " + ", ".join(known_appliers()),
        )
    return applier_registry.create(name, config, **options)


def known_appliers() -> list[str]:
    return sorted(set(_MODULES) | set(applier_registry.available()))


__all__ = ["Applier", "get_applier", "known_appliers", "applier_registry"]
