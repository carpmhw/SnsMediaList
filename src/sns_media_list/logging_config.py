"""Privacy-aware application and server logging helpers."""

import logging
import re
from typing import Any

_TOKEN_PATH = re.compile(r"(/api/media/)[^/?\s]+(/(?:preview|download))")


def build_event(
    *,
    request_id: str,
    platform: str | None,
    outcome: str,
    duration_ms: float,
    item_count: int | None = None,
    **_sensitive: Any,
) -> dict[str, Any]:
    """Build an event containing only the approved observability fields."""
    event: dict[str, Any] = {
        "request_id": request_id,
        "platform": platform,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    if item_count is not None:
        event["item_count"] = item_count
    return event


class PrivacyFilter(logging.Filter):
    """Redact token-bearing access paths and query strings in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate a log record to remove URL tokens and query strings."""
        message = record.getMessage()
        message = _TOKEN_PATH.sub(r"\1[redacted]\2", message)
        message = re.sub(r"\?[^\s\"]*", "", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging() -> None:
    """Install the privacy filter on application and Uvicorn access loggers."""
    for logger_name in ("sns_media_list", "uvicorn.access"):
        logging.getLogger(logger_name).addFilter(PrivacyFilter())
