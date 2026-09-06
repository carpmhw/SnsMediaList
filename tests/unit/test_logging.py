"""Tests for privacy-aware structured logging."""

import json
import logging

import pytest

from sns_media_list.logging_config import PrivacyFilter, build_event, configure_logging


def test_build_event_omits_sensitive_fields() -> None:
    """Verify structured events only contain permitted observability fields."""
    event = build_event(
        request_id="request-1",
        platform="x",
        outcome="success",
        duration_ms=12.5,
        item_count=2,
        source_url="https://x.com/user/status/123",
        range_url="https://pbs.twimg.com/video.mp4?token=secret",
        token="secret-token",
        cookie="private-cookie",
        authorization="Bearer private-token",
        proxy_authorization="Basic private-proxy-token",
        etag='"private-etag"',
        resolved_ip="192.0.2.1",
        transport_exception="private transport detail",
        description="private description",
        resume_attempt=1,
    )

    assert event == {
        "request_id": "request-1",
        "platform": "x",
        "outcome": "success",
        "duration_ms": 12.5,
        "item_count": 2,
        "resume_attempt": 1,
    }


def test_privacy_filter_removes_token_bearing_access_paths() -> None:
    """Verify access log records do not retain media tokens or query strings."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="test",
        lineno=1,
        msg='"GET /api/media/secret-token/download?x=1 HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )

    assert PrivacyFilter().filter(record) is True
    assert "secret-token" not in record.getMessage()
    assert "?" not in record.getMessage()


@pytest.mark.parametrize(
    "name",
    [
        "extraction_complete",
        "media_download_started",
        "media_download_completed",
        "media_download_failed",
        "media_download_aborted",
        "media_download_resume_attempted",
        "media_download_resume_succeeded",
        "media_download_resume_failed",
    ],
)
def test_default_safe_output(name, capsys) -> None:
    """重複初始化仍只輸出一次 allowlist JSON，且不序列化敏感資料。"""
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    configure_logging()
    configure_logging()
    logger = logging.getLogger("sns_media_list")
    secret = "PRIVATE_SENTINEL"
    event = build_event(
        request_id="request-1",
        platform="instagram",
        outcome="failed",
        duration_ms=2.0,
        bytes_streamed=42,
        media_class="video",
        reason_code="upstream_truncation",
        resume_attempt=1,
    )
    sensitive_fields = {
        "source_url",
        "range_url",
        "query",
        "token",
        "cookie",
        "authorization",
        "proxy_authorization",
        "etag",
        "resolved_ip",
        "transport_exception",
    }
    event.update(dict.fromkeys(sensitive_fields, secret))
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        logger.info(name, extra={"event": event, "cookie": secret}, exc_info=True, stack_info=True)
    logger.info(secret, extra={"event": event})
    output = capsys.readouterr().err
    assert secret not in output
    assert len(output.splitlines()) == 1
    parsed = json.loads(output)
    assert parsed == {
        "event": name,
        **{k: v for k, v in event.items() if k not in sensitive_fields},
    }
    assert (root.level, root.handlers) == before
    assert logger.propagate is False


def test_formatter_failure_is_silent(monkeypatch, capsys) -> None:
    """formatter failure 不得觸發 logging 的 raw record traceback。"""
    configure_logging()
    logger = logging.getLogger("sns_media_list")

    def fail(_record):
        """模擬含敏感訊息的 formatter failure。"""
        raise RuntimeError("PRIVATE_SENTINEL")

    for handler in logger.handlers:
        monkeypatch.setattr(handler, "format", fail)
    logger.info("media_download_failed", extra={"event": {}})
    assert capsys.readouterr().err == ""
