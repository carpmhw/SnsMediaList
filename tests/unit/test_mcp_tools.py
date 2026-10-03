"""透過官方 SDK Client 驗證 MCP tool 的公開契約與錯誤遮蔽。"""

import asyncio
import json
import logging

import pytest
from mcp import Client

from sns_media_list.api.limits import RequestLimiter
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.logging_config import configure_logging
from sns_media_list.services.extraction_coordinator import ExtractionCoordinator
from tests.mcp_helpers import PRIVATE_SOURCE, X_URL, FakeExtractor, make_service


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize(
    "url",
    [
        X_URL,
        "https://www.instagram.com/p/example/",
        "https://www.instagram.com/reel/example/",
        "https://www.instagram.com/stories/example/123/",
    ],
)
async def test_tool_uses_existing_pipeline_with_public_schema(url: str, mode: str) -> None:
    """兩個協定世代皆使用正式 service，不直接暴露 upstream 或 media binary。"""
    from sns_media_list.mcp.server import create_mcp_server

    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(
        settings=settings, extractor=FakeExtractor(media_types=("image", "video"))
    )
    coordinator = ExtractionCoordinator(
        service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)
    )
    server = create_mcp_server(coordinator, settings=settings)
    root_level = logging.getLogger().level
    async with Client(server, mode=mode, cache=None) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["extract_media"]
        tool = tools.tools[0]
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["additionalProperties"] is False
        result = await client.call_tool("extract_media", {"url": url})
    assert server.name == "SNS Media List"
    assert logging.getLogger().level == root_level
    assert extractor.calls == 1
    assert result.is_error is False
    assert result.structured_content["post_url"] == url
    assert result.structured_content["media"][0]["download_url"].startswith("/api/media/")
    assert len(result.structured_content["media"]) == (1 if "/stories/" in url else 2)
    assert PRIVATE_SOURCE not in result.model_dump_json()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"url": ""},
        {"url": "SENSITIVE" * 300},
        {"url": 123},
        {"url": X_URL, "SENSITIVE": "cookie"},
    ],
)
async def test_tool_validation_does_not_echo_arguments(arguments: dict[str, object]) -> None:
    """原始 arguments 與未知欄位名稱不進入公開錯誤或 SDK 日誌。"""
    from sns_media_list.mcp.server import create_mcp_server

    service, extractor = make_service()
    coordinator = ExtractionCoordinator(
        service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)
    )
    async with Client(create_mcp_server(coordinator, settings=Settings()), cache=None) as client:
        result = await client.call_tool("extract_media", arguments)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == "invalid_request"
    assert "SENSITIVE" not in result.model_dump_json()
    assert result.structured_content is None
    assert extractor.calls == 0


async def test_invalid_arguments_emit_started_and_one_terminal_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schema validation 失敗仍有安全的 started／failed 事件配對。"""
    from sns_media_list.mcp.tools import execute_tool

    configure_logging()
    service, _ = make_service()
    result = await execute_tool(
        ExtractionCoordinator(service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)),
        {"url": "", "SENSITIVE_COOKIE": "secret"},
        request_id="SENSITIVE_REQUEST_ID",
    )
    assert result.is_error is True
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [event["event"] for event in events] == [
        "mcp_extraction_started",
        "mcp_extraction_failed",
    ]
    assert events[-1]["reason_code"] == "invalid_request"
    assert events[0]["request_id"] == "unknown"
    assert "SENSITIVE" not in json.dumps(events)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "https://localhost/",
        "https://www.instagram.com/stories/example/",
        "https://x.com/example/",
    ],
)
async def test_tool_does_not_bypass_url_policy(url: str) -> None:
    """私有與不支援 URL 在既有 validator 被拒絕，不啟動 extractor。"""
    from sns_media_list.mcp.server import create_mcp_server

    service, extractor = make_service()
    coordinator = ExtractionCoordinator(
        service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)
    )
    async with Client(create_mcp_server(coordinator, settings=Settings()), cache=None) as client:
        result = await client.call_tool("extract_media", {"url": url})
    assert json.loads(result.content[0].text)["code"] == "unsupported_url"
    assert extractor.calls == 0


@pytest.mark.parametrize(
    "code",
    [
        "story_auth_required",
        "story_unavailable",
        "post_unavailable",
        "platform_authentication_failed",
        "upstream_rate_limited",
        "extraction_timeout",
        "no_media",
        "extraction_limit_exceeded",
        "capacity_exceeded",
        "unsafe_destination",
        "extraction_failed",
    ],
)
async def test_app_error_mapping_uses_fixed_public_messages(code: str) -> None:
    """任何 AppError raw message／stage 均不能沿 SDK content 外洩。"""
    from sns_media_list.mcp.server import create_mcp_server

    service, _ = make_service(
        extractor=FakeExtractor(error=AppError(code, "SENSITIVE", failure_stage="extractor_io"))
    )
    coordinator = ExtractionCoordinator(
        service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)
    )
    async with Client(create_mcp_server(coordinator, settings=Settings()), cache=None) as client:
        result = await client.call_tool("extract_media", {"url": X_URL})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == code
    assert "SENSITIVE" not in result.model_dump_json()
    assert "failure_stage" not in result.model_dump_json()


async def test_unknown_exception_is_masked_and_slot_is_reusable() -> None:
    """未知例外有固定公開分類且不永久占用 extraction lease。"""
    from sns_media_list.mcp.server import create_mcp_server

    service, _ = make_service(extractor=FakeExtractor(error=RuntimeError("SENSITIVE")))
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    async with Client(
        create_mcp_server(ExtractionCoordinator(service, limiter=limiter), settings=Settings()),
        cache=None,
    ) as client:
        result = await client.call_tool("extract_media", {"url": X_URL})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == "extraction_failed"
    assert "SENSITIVE" not in result.model_dump_json()
    async with limiter.acquire_extraction("rest-client"):
        pass


async def test_cancelled_tool_logs_one_aborted_terminal_and_releases_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """取消沿 async task 傳遞，真正 stderr 只記安全 started／aborted。"""
    from sns_media_list.mcp.tools import execute_tool

    configure_logging()
    service, extractor = make_service(extractor=FakeExtractor(blocking=True))
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    task = asyncio.create_task(
        execute_tool(ExtractionCoordinator(service, limiter=limiter), {"url": X_URL})
    )
    await extractor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with limiter.acquire_extraction("rest-client"):
        pass
    stderr = capsys.readouterr().err
    events = [json.loads(line) for line in stderr.splitlines()]
    assert [item["event"] for item in events] == [
        "mcp_extraction_started",
        "mcp_extraction_aborted",
    ]
    assert events[-1]["reason_code"] == "cancelled"
    assert X_URL not in stderr
    assert PRIVATE_SOURCE not in stderr


@pytest.mark.parametrize(
    "error", [None, AppError("post_unavailable", "SENSITIVE"), RuntimeError("SENSITIVE")]
)
async def test_real_mcp_terminal_logs_are_safe(
    error: Exception | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功與錯誤的真正 stderr 均只包含白名單欄位及唯一 terminal。"""
    from sns_media_list.mcp.tools import execute_tool

    configure_logging()
    service, _ = make_service(extractor=FakeExtractor(error=error))
    await execute_tool(
        ExtractionCoordinator(service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)),
        {"url": X_URL},
        request_id="SENSITIVE",
    )
    stderr = capsys.readouterr().err
    events = [json.loads(line) for line in stderr.splitlines()]
    assert len(events) == 2
    assert events[0]["event"] == "mcp_extraction_started"
    assert events[1]["event"] == ("mcp_extraction_failed" if error else "mcp_extraction_complete")
    assert all(item["request_id"] == "unknown" for item in events)
    assert all(
        value not in stderr for value in (X_URL, PRIVATE_SOURCE, "SENSITIVE", "download_url")
    )
