"""Application error types and HTTP status mapping."""

from dataclasses import dataclass

ERROR_STATUS: dict[str, int] = {
    "invalid_url": 400,
    "unsupported_url": 400,
    "post_unavailable": 404,
    "story_unavailable": 404,
    "token_not_found": 404,
    "no_media": 422,
    "extraction_limit_exceeded": 422,
    "local_rate_limited": 429,
    "upstream_rate_limited": 429,
    "upstream_media_invalid": 502,
    "extraction_failed": 502,
    "capacity_exceeded": 503,
    "platform_authentication_failed": 503,
    "extraction_timeout": 504,
    "token_expired": 410,
    "unsafe_destination": 502,
}


@dataclass(eq=False)
class AppError(Exception):
    """Represent a safe, stable error returned by the application."""

    code: str
    message: str
    status_code: int | None = None
    retry_after: int | None = None
    deterministic: bool = False

    def __post_init__(self) -> None:
        """Fill the status from the central error contract when omitted."""
        super().__init__(self.message)
        if self.status_code is None:
            self.status_code = ERROR_STATUS.get(self.code, 500)
