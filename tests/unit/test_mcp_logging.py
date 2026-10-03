"""MCP event builder／formatter 的白名單與雙重去識別驗證。"""

import io
import json
import logging

import pytest

from sns_media_list.logging_config import SafeEventFormatter


@pytest.mark.parametrize(
    ("event", "outcome", "optional"),
    [
        ("mcp_server_started", "success", {}),
        ("mcp_extraction_started", "started", {}),
        ("mcp_extraction_complete", "success", {"item_count": 1}),
        ("mcp_extraction_failed", "failed", {"reason_code": "extraction_failed"}),
        ("mcp_extraction_aborted", "aborted", {"reason_code": "cancelled"}),
    ],
)
def test_mcp_builder_and_formatter_allow_only_safe_fields(
    event: str,
    outcome: str,
    optional: dict[str, object],
) -> None:
    """直接 builder 與偽造 record 的敏感 extra 都被各自過濾。"""
    from sns_media_list.logging_config import build_mcp_event

    fields = build_mcp_event(
        event,
        request_id="a" * 32,
        platform="x",
        outcome=outcome,
        duration_ms=1.0,
        **optional,
        cookie="SENSITIVE",
        url="SENSITIVE",
        public_base_url="https://SENSITIVE_BASE.example",
        preview_url="https://SENSITIVE_BASE.example/api/media/SENSITIVE_TOKEN/preview",
        download_url="https://SENSITIVE_BASE.example/api/media/SENSITIVE_TOKEN/download",
    )
    assert "SENSITIVE" not in json.dumps(fields)
    record = logging.LogRecord("sns_media_list", logging.INFO, "", 0, event, (), None)
    record.event = {
        **fields,
        "source_url": "SENSITIVE",
        "token": "SENSITIVE",
        "failure_stage": "SENSITIVE",
        "public_base_url": "https://SENSITIVE_BASE.example",
        "preview_url": "https://SENSITIVE_BASE.example/api/media/SENSITIVE_TOKEN/preview",
        "download_url": "https://SENSITIVE_BASE.example/api/media/SENSITIVE_TOKEN/download",
    }
    serialized = SafeEventFormatter().format(record)
    assert json.loads(serialized)["event"] == event
    assert "SENSITIVE" not in serialized


@pytest.mark.parametrize(
    "patch",
    [
        {"platform": "SENSITIVE"},
        {"duration_ms": float("nan")},
        {"duration_ms": -1},
        {"reason_code": "SENSITIVE"},
        {"outcome": "SENSITIVE"},
    ],
)
def test_mcp_formatter_rejects_untrusted_field_values(patch: dict[str, object]) -> None:
    """Record 即使繞過 builder，也不能序列化未知 enum 或非有限數值。"""
    record = logging.LogRecord(
        "sns_media_list", logging.INFO, "", 0, "mcp_extraction_failed", (), None
    )
    record.event = {
        "request_id": "a" * 32,
        "platform": "x",
        "duration_ms": 1.0,
        "outcome": "failed",
        "reason_code": "extraction_failed",
        **patch,
    }
    assert SafeEventFormatter().format(record) == ""


def test_sdk_logging_discards_preexisting_sdk_handlers() -> None:
    """既存 SDK／子 logger handler 不得輸出 raw Host、session 或例外。"""
    from sns_media_list.logging_config import configure_mcp_logging

    output = io.StringIO()
    loggers = [logging.getLogger("mcp"), logging.getLogger("mcp.server.transport_security")]
    for logger in loggers:
        logger.disabled = False
        logger.addHandler(logging.StreamHandler(output))
    configure_mcp_logging()
    for logger in loggers:
        logger.warning("Invalid Host header: SENSITIVE_SESSION")
    assert output.getvalue() == ""
