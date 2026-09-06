"""Privacy-aware application and server logging helpers."""

import json
import logging
import re
import sys
from typing import Any

_TOKEN_PATH = re.compile(r"(/api/media/)[^/?\s]+(/(?:preview|download))")
_EVENT_NAMES = frozenset(
    {
        "extraction_complete",
        "media_download_started",
        "media_download_completed",
        "media_download_failed",
        "media_download_aborted",
        "media_download_resume_attempted",
        "media_download_resume_succeeded",
        "media_download_resume_failed",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "request_id",
        "platform",
        "outcome",
        "duration_ms",
        "item_count",
        "media_class",
        "bytes_streamed",
        "reason_code",
        "resume_attempt",
    }
)


class SafeEventFormatter(logging.Formatter):
    """只序列化固定事件名稱與核准欄位，不處理 raw message 或例外鏈。"""

    def format(self, record: logging.LogRecord) -> str:
        """拒絕未知事件與非結構化內容，避免任意 LogRecord extra 外洩。"""
        if not isinstance(record.msg, str) or record.msg not in _EVENT_NAMES:
            return ""
        event = getattr(record, "event", None)
        if not isinstance(event, dict):
            return ""
        fields = {key: value for key, value in event.items() if key in _EVENT_FIELDS}
        return json.dumps({"event": record.msg, **fields}, allow_nan=False)


class SafeEventHandler(logging.Handler):
    """將安全 JSON 寫入 stderr，且輸出失敗時不傾印原始 LogRecord。"""

    def emit(self, record: logging.LogRecord) -> None:
        """隔離 formatter 與 stderr 失敗，避免 logging 覆蓋傳輸錯誤。"""
        try:
            message = self.format(record)
            if message:
                sys.stderr.write(message + "\n")
                sys.stderr.flush()
        except Exception:
            pass


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
    resume_attempt: int | None = None,
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
    if resume_attempt is not None:
        event["resume_attempt"] = resume_attempt
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
    """冪等配置 application INFO 輸出，不更動 root 或啟用 access log。"""
    logger = logging.getLogger("sns_media_list")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(handler, SafeEventHandler) for handler in logger.handlers):
        handler = SafeEventHandler()
        handler.setFormatter(SafeEventFormatter())
        logger.addHandler(handler)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, PrivacyFilter) for item in access.filters):
        access.addFilter(PrivacyFilter())
