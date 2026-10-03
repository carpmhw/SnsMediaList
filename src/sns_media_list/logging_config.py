"""Privacy-aware application and server logging helpers."""

import json
import logging
import math
import re
import sys
from collections.abc import Mapping
from typing import Any, cast

from .errors import (
    FailureStageValue,
    normalize_extractor_diagnostics,
    normalize_failure_stage,
)

_TOKEN_PATH = re.compile(r"(/api/media/)[^/?\s]+(/(?:preview|download))")
_REQUEST_ID = re.compile(r"(?:[0-9a-f]{32}|unknown)")
_EXTRACTION_EVENTS = frozenset({"extraction_complete", "extraction_failed"})
_MCP_OUTCOMES = {
    "mcp_server_started": "success",
    "mcp_server_failed": "failed",
    "mcp_extraction_started": "started",
    "mcp_extraction_complete": "success",
    "mcp_extraction_failed": "failed",
    "mcp_extraction_aborted": "aborted",
}
EXTRACTION_REASON_CODES = frozenset(
    {
        "invalid_url",
        "unsupported_url",
        "post_unavailable",
        "story_auth_required",
        "story_unavailable",
        "platform_authentication_failed",
        "local_rate_limited",
        "upstream_rate_limited",
        "extraction_timeout",
        "extraction_failed",
        "no_media",
        "extraction_limit_exceeded",
        "capacity_exceeded",
        "unsafe_destination",
    }
)
_EVENT_NAMES = frozenset(
    {
        "extraction_complete",
        "extraction_failed",
        "media_download_started",
        "media_download_completed",
        "media_download_failed",
        "media_download_aborted",
        "media_download_resume_attempted",
        "media_download_resume_succeeded",
        "media_download_resume_failed",
    }
    | _MCP_OUTCOMES.keys()
)
_EXTRACTOR_DIAGNOSTIC_FIELDS = (
    "extractor_diagnostic_source",
    "extractor_error_type",
    "extractor_exit_code",
    "extractor_http_statuses",
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
        "failure_stage",
        "resume_attempt",
        *_EXTRACTOR_DIAGNOSTIC_FIELDS,
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
        if record.msg in _MCP_OUTCOMES:
            mcp_fields = _validated_mcp_fields(record.msg, fields)
            if mcp_fields is None:
                return ""
            return json.dumps({"event": record.msg, **mcp_fields}, allow_nan=False)
        if record.msg in _EXTRACTION_EVENTS:
            validated_fields = _validated_extraction_fields(record.msg, fields)
            if validated_fields is None:
                return ""
            fields = validated_fields
        else:
            fields.pop("failure_stage", None)
            for field_name in _EXTRACTOR_DIAGNOSTIC_FIELDS:
                fields.pop(field_name, None)
        return json.dumps({"event": record.msg, **fields}, allow_nan=False)


def _validated_extraction_fields(
    event_name: str,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """驗證 extraction event 的欄位形狀與值皆符合固定安全契約。"""
    request_id = fields.get("request_id")
    platform = fields.get("platform")
    outcome = fields.get("outcome")
    duration_ms = fields.get("duration_ms")
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        request_id = "unknown"
    if (
        (platform is not None and (type(platform) is not str or platform not in {"instagram", "x"}))
        or type(outcome) is not str
        or type(duration_ms) not in {int, float}
    ):
        return None
    numeric_duration = cast(int | float, duration_ms)
    if numeric_duration < 0:
        return None
    try:
        if not math.isfinite(numeric_duration):
            return None
    except OverflowError:
        return None

    result: dict[str, Any] = {
        "request_id": request_id,
        "platform": platform,
        "outcome": outcome,
        "duration_ms": numeric_duration,
    }
    if event_name == "extraction_complete":
        item_count = fields.get("item_count")
        if outcome != "success" or type(item_count) is not int or item_count < 0:
            return None
        if "reason_code" in fields:
            return None
        result["item_count"] = item_count
        return result

    reason_code = fields.get("reason_code")
    if (
        outcome != "failed"
        or type(reason_code) is not str
        or reason_code not in EXTRACTION_REASON_CODES
        or "item_count" in fields
    ):
        return None
    result["reason_code"] = reason_code
    failure_stage = normalize_failure_stage(fields.get("failure_stage"))
    if failure_stage is not None:
        result["failure_stage"] = failure_stage
    result.update(_normalized_extractor_diagnostic_fields(fields))
    return result


def _normalized_extractor_diagnostic_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """以共用 metadata 規則重驗 extractor 欄位並建立 JSON 安全值。"""
    diagnostics = normalize_extractor_diagnostics(
        extractor_diagnostic_source=fields.get("extractor_diagnostic_source"),
        extractor_error_type=fields.get("extractor_error_type"),
        extractor_exit_code=fields.get("extractor_exit_code"),
        extractor_http_statuses=fields.get("extractor_http_statuses"),
    )
    normalized: dict[str, Any] = {}
    if diagnostics.extractor_diagnostic_source is not None:
        normalized["extractor_diagnostic_source"] = diagnostics.extractor_diagnostic_source
    if diagnostics.extractor_error_type is not None:
        normalized["extractor_error_type"] = diagnostics.extractor_error_type
    if diagnostics.extractor_exit_code is not None:
        normalized["extractor_exit_code"] = diagnostics.extractor_exit_code
    if diagnostics.extractor_http_statuses is not None:
        normalized["extractor_http_statuses"] = list(diagnostics.extractor_http_statuses)
    return normalized


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
    failure_stage: FailureStageValue | None = None,
    resume_attempt: int | None = None,
    extractor_diagnostic_source: object = None,
    extractor_error_type: object = None,
    extractor_exit_code: object = None,
    extractor_http_statuses: object = None,
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
    normalized_stage = normalize_failure_stage(failure_stage)
    if outcome == "failed" and normalized_stage is not None:
        event["failure_stage"] = normalized_stage
    if resume_attempt is not None:
        event["resume_attempt"] = resume_attempt
    if outcome == "failed" and type(reason_code) is str and reason_code in EXTRACTION_REASON_CODES:
        event.update(
            _normalized_extractor_diagnostic_fields(
                {
                    "extractor_diagnostic_source": extractor_diagnostic_source,
                    "extractor_error_type": extractor_error_type,
                    "extractor_exit_code": extractor_exit_code,
                    "extractor_http_statuses": extractor_http_statuses,
                }
            )
        )
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


def _validated_mcp_fields(
    event_name: str, fields: Mapping[str, object]
) -> dict[str, object] | None:
    """依事件驗證 MCP enum／有限數值，回傳不含任何私有 extra 的新字典。"""
    if event_name not in _MCP_OUTCOMES or fields.get("outcome") != _MCP_OUTCOMES[event_name]:
        return None
    platform = fields.get("platform")
    duration = fields.get("duration_ms")
    if platform is not None and (type(platform) is not str or platform not in {"instagram", "x"}):
        return None
    if type(duration) not in {int, float}:
        return None
    numeric_duration = cast(int | float, duration)
    try:
        if numeric_duration < 0 or not math.isfinite(numeric_duration):
            return None
    except OverflowError:
        return None
    request_id = fields.get("request_id")
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        request_id = "unknown"
    result: dict[str, object] = {
        "request_id": request_id,
        "platform": platform,
        "outcome": _MCP_OUTCOMES[event_name],
        "duration_ms": numeric_duration,
    }
    if event_name == "mcp_extraction_complete":
        count = fields.get("item_count")
        if type(count) is not int or not 0 <= count <= 20:
            return None
        result["item_count"] = count
    elif event_name in {"mcp_extraction_failed", "mcp_extraction_aborted", "mcp_server_failed"}:
        reason = fields.get("reason_code")
        allowed = (
            EXTRACTION_REASON_CODES | {"invalid_request"}
            if event_name == "mcp_extraction_failed"
            else {"cancelled"}
            if event_name == "mcp_extraction_aborted"
            else {"startup_failed"}
        )
        if type(reason) is not str or reason not in allowed:
            return None
        result["reason_code"] = reason
    return result


def build_mcp_event(event_name: str, **fields: object) -> dict[str, object]:
    """在建立 event 時先套用相同白名單，未知資料不流向 logger。"""
    return _validated_mcp_fields(event_name, fields) or {}


def log_mcp_event(event_name: str, **fields: object) -> None:
    """發出受控 MCP JSON 事件，logging 失敗不覆蓋操作結果或取消。"""
    event = build_mcp_event(event_name, **fields)
    if event:
        try:
            logging.getLogger("sns_media_list.mcp").info(event_name, extra={"event": event})
        except Exception:
            pass


def configure_mcp_logging() -> None:
    """隔離 SDK 與既存子 logger handlers，觀測僅用 application 白名單事件。"""
    names = {
        "mcp",
        *(name for name in logging.Logger.manager.loggerDict if name.startswith("mcp.")),
    }
    for name in names:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True


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
