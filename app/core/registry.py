"""Generic plugin registry.

Every pluggable family in this app (SCM connectors, quality connectors,
reviewers) is a `Registry`. Implementations register themselves by name at
import time; the composition root only imports the packages the effective
config asks for, so a disabled plugin is never constructed and a removed
plugin cannot break the core.
"""

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator: register a factory (usually the class itself) under `name`."""

        def decorator(factory: Callable[..., T]) -> Callable[..., T]:
            if name in self._factories:
                raise ValueError(f"{self.kind} {name!r} is already registered")
            self._factories[name] = factory
            return factory

        return decorator

    def create(self, name: str, *args, **kwargs) -> T:
        if name not in self._factories:
            available = ", ".join(self.available()) or "(none)"
            raise ValueError(f"Unknown {self.kind}: {name!r}. Available: {available}")
        return self._factories[name](*args, **kwargs)

    def available(self) -> list[str]:
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories
