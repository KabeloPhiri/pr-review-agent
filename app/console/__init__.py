"""Admin console for the PR review agent: usage, quality and settings.

Mounted by `app/server.py`. Nothing outside this package imports it; the
review path only knows `app.core.settings_store`.
"""

from app.console.routes import build_console_router

__all__ = ["build_console_router"]
