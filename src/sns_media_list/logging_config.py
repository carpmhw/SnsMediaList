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
    media_class: str | None = None,
    bytes_streamed: int | None = None,
    reason_code: str | None = None,
    **_sensitive: Any,
) -> dict[str, Any]:
    """建立只包含核准觀測欄位的結構化事件。"""
    event: dict[str, Any] = {
        "request_id": request_id,
        "platform": platform,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    if item_count is not None:
        event["item_count"] = item_count
    if media_class is not None:
        event["media_class"] = media_class
    if bytes_streamed is not None:
        event["bytes_streamed"] = bytes_streamed
    if reason_code is not None:
        event["reason_code"] = reason_code
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
