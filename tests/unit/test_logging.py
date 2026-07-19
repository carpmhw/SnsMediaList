"""Tests for privacy-aware structured logging."""

import logging

from sns_media_list.logging_config import PrivacyFilter, build_event


def test_build_event_omits_sensitive_fields() -> None:
    """Verify structured events only contain permitted observability fields."""
    event = build_event(
        request_id="request-1",
        platform="x",
        outcome="success",
        duration_ms=12.5,
        item_count=2,
        source_url="https://x.com/user/status/123",
        token="secret-token",
        description="private description",
    )

    assert event == {
        "request_id": "request-1",
        "platform": "x",
        "outcome": "success",
        "duration_ms": 12.5,
        "item_count": 2,
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
