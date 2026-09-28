"""Tests for privacy-aware structured logging."""

import json
import logging
import sys

import pytest

from sns_media_list.logging_config import (
    PrivacyFilter,
    SafeEventFormatter,
    SafeEventHandler,
    build_event,
    configure_logging,
)


def test_build_event_omits_sensitive_fields() -> None:
    """驗證結構化事件僅包含核准觀測欄位並可攜帶有限失敗 stage。"""
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


_FAILURE_STAGES = (
    "extractor_start",
    "extractor_timeout",
    "extractor_output_limit",
    "extractor_io",
    "extractor_process_unclassified",
    "extractor_invalid_output",
    "extractor_empty_output",
    "extractor_no_media",
    "extractor_platform_error",
    "unknown",
)
_UNSAFE_FAILURE_STAGE = (
    "https://example.test/PRIVATE_USERNAME/PRIVATE_STORY_ID"
    "?cookie=PRIVATE_COOKIE&authorization=PRIVATE_AUTHORIZATION&token=PRIVATE_TOKEN"
)
_FAILURE_STAGE_CASES = [
    *(pytest.param(stage, stage, id=stage) for stage in _FAILURE_STAGES),
    pytest.param(None, None, id="none"),
    pytest.param(_UNSAFE_FAILURE_STAGE, "unknown", id="unsafe-url"),
    pytest.param("../../PRIVATE_PATH", "unknown", id="unsafe-path"),
    pytest.param(object(), "unknown", id="non-string"),
]


@pytest.mark.parametrize(("failure_stage", "expected_stage"), _FAILURE_STAGE_CASES)
def test_build_event_normalizes_failure_stage_and_drops_sensitive_sentinels(
    failure_stage: object,
    expected_stage: str | None,
) -> None:
    """驗證 event builder 僅保留有限 stage 並排除敏感診斷 sentinel。"""
    event = build_event(
        request_id="a" * 32,
        platform="instagram",
        outcome="failed",
        duration_ms=1,
        reason_code="extraction_failed",
        failure_stage=failure_stage,
        source_url="PRIVATE_SOURCE_URL",
        username="PRIVATE_USERNAME",
        story_id="PRIVATE_STORY_ID",
        cookie="PRIVATE_COOKIE",
        authorization="PRIVATE_AUTHORIZATION",
        token="PRIVATE_TOKEN",
        command="PRIVATE_COMMAND",
        stdout="PRIVATE_STDOUT",
        stderr="PRIVATE_STDERR",
    )

    if expected_stage is None:
        assert "failure_stage" not in event
    else:
        assert event["failure_stage"] == expected_stage
    serialized = json.dumps(event)
    for sentinel in (
        "PRIVATE_SOURCE_URL",
        "PRIVATE_USERNAME",
        "PRIVATE_STORY_ID",
        "PRIVATE_COOKIE",
        "PRIVATE_AUTHORIZATION",
        "PRIVATE_TOKEN",
        "PRIVATE_COMMAND",
        "PRIVATE_STDOUT",
        "PRIVATE_STDERR",
        _UNSAFE_FAILURE_STAGE,
        "PRIVATE_PATH",
    ):
        assert sentinel not in serialized


@pytest.mark.parametrize(
    ("diagnostic_fields", "expected_fields"),
    [
        pytest.param(
            {
                "extractor_diagnostic_source": "stderr",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 0,
                "extractor_http_statuses": [403, 401, 403],
            },
            {
                "extractor_diagnostic_source": "stderr",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 0,
                "extractor_http_statuses": [401, 403],
            },
            id="valid-values",
        ),
        pytest.param(
            {
                "extractor_diagnostic_source": "PRIVATE_URL_COOKIE_TOKEN",
                "extractor_error_type": "PRIVATE_RAW_TYPE",
            },
            {
                "extractor_diagnostic_source": "unknown",
                "extractor_error_type": "unknown",
            },
            id="untrusted-enums",
        ),
        pytest.param(
            {"extractor_exit_code": True, "extractor_http_statuses": [403, "PRIVATE_STATUS"]},
            {},
            id="invalid-optional-values",
        ),
        pytest.param(
            {"extractor_exit_code": -9, "extractor_http_statuses": iter([429])},
            {"extractor_exit_code": -9},
            id="bounded-negative-exit-and-custom-iterator",
        ),
    ],
)
def test_build_event_normalizes_extractor_diagnostic_value_matrix(
    diagnostic_fields: dict[str, object],
    expected_fields: dict[str, object],
) -> None:
    """驗證 builder 正規化 optional 診斷欄位且不丟棄合法失敗事件。"""
    event = build_event(
        request_id="a" * 32,
        platform="instagram",
        outcome="failed",
        duration_ms=1,
        reason_code="extraction_failed",
        failure_stage="extractor_process_unclassified",
        **diagnostic_fields,
    )

    assert event["reason_code"] == "extraction_failed"
    assert event["failure_stage"] == "extractor_process_unclassified"
    for field_name, expected_value in expected_fields.items():
        assert event[field_name] == expected_value
    for field_name in {
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    } - expected_fields.keys():
        assert field_name not in event
    serialized = json.dumps(event)
    assert "PRIVATE_URL_COOKIE_TOKEN" not in serialized
    assert "PRIVATE_RAW_TYPE" not in serialized
    assert "PRIVATE_STATUS" not in serialized


def test_build_event_omits_failure_stage_for_success() -> None:
    """驗證成功 extraction event 不包含 failure stage。"""
    event = build_event(
        request_id="a" * 32,
        platform="instagram",
        outcome="success",
        duration_ms=1,
        item_count=1,
        failure_stage="extractor_io",
        extractor_diagnostic_source="stderr",
        extractor_error_type="unknown",
        extractor_exit_code=0,
        extractor_http_statuses=[403],
    )

    assert "failure_stage" not in event
    assert not {
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    }.intersection(event)


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
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
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


@pytest.mark.parametrize(
    ("event_name", "event"),
    [
        pytest.param(
            "extraction_complete",
            {
                "request_id": "a" * 32,
                "platform": "instagram",
                "outcome": "success",
                "duration_ms": 12.5,
                "item_count": 2,
            },
            id="completion",
        ),
        pytest.param(
            "extraction_failed",
            {
                "request_id": "00000000000040008000000000000001",
                "platform": "x",
                "outcome": "failed",
                "duration_ms": 0,
                "reason_code": "story_auth_required",
            },
            id="failure",
        ),
    ],
)
def test_extraction_events_emit_only_allowlisted_fields(
    event_name: str,
    event: dict[str, object],
) -> None:
    """驗證擷取事件只輸出符合契約的欄位與合法欄位值。"""
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.INFO,
        pathname="test",
        lineno=1,
        msg=event_name,
        args=(),
        exc_info=None,
    )
    record.event = {**event, "source_url": "PRIVATE_URL", "cookie": "PRIVATE_COOKIE"}

    formatted = SafeEventFormatter().format(record)

    assert formatted
    assert json.loads(formatted) == {"event": event_name, **event}
    assert "PRIVATE_" not in formatted


@pytest.mark.parametrize(
    ("diagnostic_fields", "expected_fields"),
    [
        pytest.param(
            {
                "extractor_diagnostic_source": "datajob_error",
                "extractor_error_type": "http_error",
                "extractor_exit_code": 0,
                "extractor_http_statuses": [403, 401, 403],
            },
            {
                "extractor_diagnostic_source": "datajob_error",
                "extractor_error_type": "http_error",
                "extractor_exit_code": 0,
                "extractor_http_statuses": [401, 403],
            },
            id="valid-values",
        ),
        pytest.param(
            {
                "extractor_diagnostic_source": "PRIVATE_COOKIE_URL_TOKEN",
                "extractor_error_type": object(),
                "extractor_exit_code": False,
                "extractor_http_statuses": iter([403]),
            },
            {
                "extractor_diagnostic_source": "unknown",
                "extractor_error_type": "unknown",
            },
            id="invalid-values-preserve-event",
        ),
    ],
)
def test_formatter_revalidates_direct_record_diagnostic_value_matrix(
    diagnostic_fields: dict[str, object],
    expected_fields: dict[str, object],
) -> None:
    """驗證直接 LogRecord 的 optional 診斷欄位也逐欄重驗。"""
    private_sentinel = "PRIVATE_RAW_MESSAGE_ARGS_EXCEPTION"
    try:
        raise RuntimeError(private_sentinel)
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.ERROR,
        pathname="test",
        lineno=1,
        msg=private_sentinel,
        args=(private_sentinel,),
        exc_info=exc_info,
    )
    record.msg = "extraction_failed"
    record.event = {
        "request_id": "a" * 32,
        "platform": "instagram",
        "outcome": "failed",
        "duration_ms": 1,
        "reason_code": "extraction_failed",
        **diagnostic_fields,
    }

    formatted = SafeEventFormatter().format(record)

    assert formatted
    event = json.loads(formatted)
    for field_name, expected_value in expected_fields.items():
        assert event[field_name] == expected_value
    for field_name in {
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    } - expected_fields.keys():
        assert field_name not in event
    assert private_sentinel not in formatted
    assert "PRIVATE_COOKIE_URL_TOKEN" not in formatted


@pytest.mark.parametrize(
    ("event_name", "event"),
    [
        pytest.param("unexpected_event", {}, id="unknown-event"),
        pytest.param(
            "extraction_failed",
            {
                "request_id": "a" * 32,
                "platform": "instagram",
                "outcome": "failed",
                "duration_ms": 1,
                "reason_code": "https://example.test/?token=secret",
            },
            id="unknown-reason",
        ),
        pytest.param(
            "extraction_complete",
            {
                "request_id": "a" * 32,
                "platform": "instagram.example",
                "outcome": "success",
                "duration_ms": 1,
                "item_count": 1,
            },
            id="invalid-platform",
        ),
        pytest.param(
            "extraction_failed",
            {
                "request_id": "a" * 32,
                "platform": "instagram",
                "outcome": "failed",
                "duration_ms": float("nan"),
                "reason_code": "story_auth_required",
            },
            id="non-finite-duration",
        ),
        pytest.param(
            "extraction_failed",
            {
                "request_id": "a" * 32,
                "platform": "instagram",
                "outcome": "failed",
                "duration_ms": 1,
                "reason_code": "story_auth_required",
                "item_count": 1,
            },
            id="failure-has-success-field",
        ),
    ],
)
def test_invalid_extraction_events_are_rejected(
    event_name: str,
    event: dict[str, object],
) -> None:
    """驗證未知事件及不符合擷取契約的欄位值不會格式化輸出。"""
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.INFO,
        pathname="test",
        lineno=1,
        msg=event_name,
        args=(),
        exc_info=None,
    )
    record.event = event

    assert SafeEventFormatter().format(record) == ""


def test_extraction_formatter_replaces_invalid_request_id() -> None:
    """驗證不合法 request ID 會降為固定 unknown 並保留安全事件。"""
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.ERROR,
        pathname="test",
        lineno=1,
        msg="extraction_failed",
        args=(),
        exc_info=None,
    )
    record.event = {
        "request_id": "request-secret",
        "platform": "instagram",
        "outcome": "failed",
        "duration_ms": 1,
        "reason_code": "story_auth_required",
    }

    formatted = SafeEventFormatter().format(record)

    assert json.loads(formatted)["request_id"] == "unknown"
    assert "request-secret" not in formatted


@pytest.mark.parametrize(("failure_stage", "expected_stage"), _FAILURE_STAGE_CASES)
def test_formatter_normalizes_direct_failure_stage_and_excludes_private_payloads(
    failure_stage: object,
    expected_stage: str | None,
) -> None:
    """驗證直接建立的 LogRecord 也正規化 stage 並排除診斷與例外鏈。"""
    exception_sentinel = "PRIVATE_EXCEPTION_CHAIN"
    try:
        raise RuntimeError(exception_sentinel)
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.ERROR,
        pathname="test",
        lineno=1,
        msg="PRIVATE_RAW_STDOUT",
        args=("PRIVATE_RAW_STDERR",),
        exc_info=exc_info,
    )
    record.msg = "extraction_failed"
    record.event = {
        "request_id": "a" * 32,
        "platform": "instagram",
        "outcome": "failed",
        "duration_ms": 1,
        "reason_code": "extraction_failed",
        "failure_stage": failure_stage,
        "source_url": "PRIVATE_SOURCE_URL",
        "username": "PRIVATE_USERNAME",
        "story_id": "PRIVATE_STORY_ID",
        "cookie": "PRIVATE_COOKIE",
        "authorization": "PRIVATE_AUTHORIZATION",
        "token": "PRIVATE_TOKEN",
        "command": "PRIVATE_COMMAND",
    }

    formatted = SafeEventFormatter().format(record)

    parsed = json.loads(formatted)
    if expected_stage is None:
        assert "failure_stage" not in parsed
    else:
        assert parsed["failure_stage"] == expected_stage
    for sentinel in (
        exception_sentinel,
        "PRIVATE_RAW_STDOUT",
        "PRIVATE_RAW_STDERR",
        "PRIVATE_SOURCE_URL",
        "PRIVATE_USERNAME",
        "PRIVATE_STORY_ID",
        "PRIVATE_COOKIE",
        "PRIVATE_AUTHORIZATION",
        "PRIVATE_TOKEN",
        "PRIVATE_COMMAND",
        _UNSAFE_FAILURE_STAGE,
        "PRIVATE_PATH",
    ):
        assert sentinel not in formatted


@pytest.mark.parametrize(
    "event_name",
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
@pytest.mark.parametrize("failure_stage", ["extractor_io", _UNSAFE_FAILURE_STAGE])
def test_formatter_removes_failure_stage_from_success_and_download_events(
    event_name: str,
    failure_stage: str,
) -> None:
    """驗證成功 extraction 與所有 download event 均移除直接注入的 stage。"""
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.INFO,
        pathname="test",
        lineno=1,
        msg=event_name,
        args=(),
        exc_info=None,
    )
    event: dict[str, object] = {
        "failure_stage": failure_stage,
        "extractor_diagnostic_source": "stderr",
        "extractor_error_type": "unknown",
        "extractor_exit_code": 0,
        "extractor_http_statuses": [403],
    }
    if event_name == "extraction_complete":
        event.update(
            {
                "request_id": "a" * 32,
                "platform": "instagram",
                "outcome": "success",
                "duration_ms": 1,
                "item_count": 1,
            }
        )
    record.event = event

    formatted = SafeEventFormatter().format(record)

    assert formatted
    assert "failure_stage" not in json.loads(formatted)
    assert not {
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    }.intersection(json.loads(formatted))
    assert _UNSAFE_FAILURE_STAGE not in formatted


def test_extraction_formatter_ignores_raw_message_and_exception() -> None:
    """驗證擷取 formatter 不序列化 raw message、args 或 exception chain。"""
    secret = "PRIVATE_RAW_DIAGNOSTIC"
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.ERROR,
        pathname="test",
        lineno=1,
        msg=secret,
        args=(secret,),
        exc_info=exc_info,
    )
    record.msg = "extraction_failed"
    record.event = {
        "request_id": "a" * 32,
        "platform": "instagram",
        "outcome": "failed",
        "duration_ms": 1,
        "reason_code": "story_auth_required",
        "raw_message": secret,
    }

    formatted = SafeEventFormatter().format(record)

    assert secret not in formatted
    assert "story_auth_required" in formatted


def test_default_extraction_events_are_written_to_stderr(capsys) -> None:
    """驗證預設 handler 將成功與失敗擷取事件寫成單行 JSON。"""
    configure_logging()
    logger = logging.getLogger("sns_media_list")
    logger.info(
        "extraction_complete",
        extra={
            "event": build_event(
                request_id="a" * 32,
                platform="instagram",
                outcome="success",
                duration_ms=2.0,
                item_count=1,
            )
        },
    )
    logger.info(
        "extraction_failed",
        extra={
            "event": build_event(
                request_id="b" * 32,
                platform="instagram",
                outcome="failed",
                duration_ms=3.0,
                reason_code="story_auth_required",
            )
        },
    )

    output = capsys.readouterr().err.splitlines()

    assert [json.loads(line)["event"] for line in output] == [
        "extraction_complete",
        "extraction_failed",
    ]


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


def test_safe_event_handler_ignores_stderr_write_failure(monkeypatch) -> None:
    """驗證 stderr 寫入失敗不會從安全 logging handler 外洩。"""

    class FailingStderr:
        """模擬不可寫入的程序 stderr。"""

        def write(self, _message: str) -> int:
            """模擬輸出錯誤並避免保留傳入內容。"""
            raise OSError("PRIVATE_STDERR_FAILURE")

        def flush(self) -> None:
            """模擬 flush 錯誤。"""
            raise OSError("PRIVATE_STDERR_FAILURE")

    monkeypatch.setattr("sns_media_list.logging_config.sys.stderr", FailingStderr())
    handler = SafeEventHandler()
    handler.setFormatter(SafeEventFormatter())
    record = logging.LogRecord(
        name="sns_media_list",
        level=logging.ERROR,
        pathname="test",
        lineno=1,
        msg="extraction_failed",
        args=(),
        exc_info=None,
    )
    record.event = {
        "request_id": "a" * 32,
        "platform": "instagram",
        "outcome": "failed",
        "duration_ms": 1,
        "reason_code": "story_auth_required",
    }

    handler.emit(record)
