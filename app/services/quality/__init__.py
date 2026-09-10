"""Quality-tool connector plugins."""

from __future__ import annotations

import importlib

from app.core.errors import ConfigError
from app.services.quality.base import QualityConnector, quality_registry

_MODULES = {
    "noop": "app.services.quality.noop",
    # "sonarqube": "app.services.quality.sonarqube",
}


def get_quality_connector(name: str, config=None, **options) -> QualityConnector:
    if name not in quality_registry and name in _MODULES:
        importlib.import_module(_MODULES[name])
    if name not in quality_registry:
        raise ConfigError(
            f"Unknown quality connector: {name!r}",
            detail="Available: " + ", ".join(known_quality_connectors()),
        )
    return quality_registry.create(name, config=config, **options)


def known_quality_connectors() -> list[str]:
    return sorted(set(_MODULES) | set(quality_registry.available()))


__all__ = [
    "QualityConnector",
    "get_quality_connector",
    "known_quality_connectors",
    "quality_registry",
]
