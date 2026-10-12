"""PR workspace: browse a pull request, apply the bot's suggestions, check
alignment, and hand the session to a phone.

Mounted by `app/server.py`. Nothing outside this package imports it; it
reaches the review path only through `ReviewPipeline`.
"""

from app.workspace.routes import build_workspace_router

__all__ = ["build_workspace_router"]
