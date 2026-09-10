"""Error types that map cleanly onto HTTP responses."""


class PrReviewError(Exception):
    """Base class. `status_code` is what the API layer returns."""

    status_code = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ConfigError(PrReviewError):
    """The effective configuration is invalid or names an unknown plugin."""

    status_code = 400


class ScmError(PrReviewError):
    """The SCM provider rejected a call or returned something unusable."""

    status_code = 502


class ScmAuthError(ScmError):
    """The supplied SCM token is missing, expired, or lacks scope."""

    status_code = 401


class ReviewerError(PrReviewError):
    """The review model could not be reached."""

    status_code = 502
