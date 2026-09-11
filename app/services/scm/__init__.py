"""SCM connector plugins.

Implementations are imported lazily so that a connector nobody selected is
never loaded — the point of the registry. Add a provider by dropping a module
in this package and listing it in `_MODULES`.
"""

from __future__ import annotations

import importlib

from app.core.errors import ConfigError
from app.services.scm.base import ScmConnector, scm_registry

_MODULES = {
    "azure_devops": "app.services.scm.azure_devops",
    "github": "app.services.scm.github",
    "fake": "app.services.scm.fake",
}


def get_connector(name: str, token: str | None = None, **options) -> ScmConnector:
    if name not in scm_registry and name in _MODULES:
        importlib.import_module(_MODULES[name])
    if name not in scm_registry:
        raise ConfigError(
            f"Unknown SCM connector: {name!r}",
            detail="Available: " + ", ".join(known_scm_connectors()),
        )
    return scm_registry.create(name, token=token, **options)


def known_scm_connectors() -> list[str]:
    """Every connector that can be selected, loaded or not."""
    return sorted(set(_MODULES) | set(scm_registry.available()))


__all__ = ["ScmConnector", "get_connector", "known_scm_connectors", "scm_registry"]
