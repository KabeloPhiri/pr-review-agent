"""Cross-site request refusal, shared by every browser-facing package.

Databricks Apps signs the browser in before a request reaches the app, so a
page on another site could still make a signed-in user's browser submit a
form or `fetch` here. Browsers say where a request came from; anything that
did not come from this app's own pages is refused.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request


def is_cross_site(request: Request) -> bool:
    """True when the request was submitted by a page on another site."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if urlparse(origin).netloc != host:
            return True
    return False
