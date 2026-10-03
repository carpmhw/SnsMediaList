"""單一 MCP 媒體擷取操作與固定公開錯誤 adapter。"""

import asyncio
import json
from time import perf_counter
from uuid import uuid4

from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from ..errors import AppError
from ..logging_config import log_mcp_event
from ..services.extraction_coordinator import ExtractionCoordinator
from ..url_validation import platform_for_url
from .models import McpExtractionInput, to_mcp_result

_PUBLIC_MESSAGES = {
    "invalid_request": "擷取參數無效；請提供一個長度 1–2048 的 URL 字串。",
    "invalid_url": "網址無效。",
    "unsupported_url": "僅接受支援的平台 HTTPS 單篇媒體網址。",
    "post_unavailable": "貼文不存在或目前無法取得。",
    "story_auth_required": "此 Story 需要 operator 配置 Instagram 驗證。",
    "story_unavailable": "此 Story 目前無法取得。",
    "platform_authentication_failed": "平台 session 無法使用，請聯絡 operator。",
    "local_rate_limited": "本機擷取額度已用盡，請稍後再試。",
    "upstream_rate_limited": "來源平台正在限流，請稍後再試。",
    "extraction_timeout": "擷取逾時。",
    "extraction_failed": "目前無法完成擷取。",
    "no_media": "沒有可下載的支援媒體。",
    "extraction_limit_exceeded": "媒體數量超過擷取上限。",
    "capacity_exceeded": "媒體 token 容量不足，請稍後再試。",
    "unsafe_destination": "來源連線未通過安全檢查。",
}


def error_result(code: str) -> CallToolResult:
    """僅序列化白名單 code 與固定 message，不使用 exception 文字。"""
    safe_code = code if code in _PUBLIC_MESSAGES else "extraction_failed"
    return CallToolResult(
        is_error=True,
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {"code": safe_code, "message": _PUBLIC_MESSAGES[safe_code]}, ensure_ascii=False
                ),
            )
        ],
    )


async def execute_tool(
    coordinator: ExtractionCoordinator,
    arguments: object,
    *,
    request_id: str | None = None,
) -> CallToolResult:
    """嚴格驗證 input，透過共享 budget 擷取，再產生安全且有唯一 terminal 的結果。"""
    request_id = request_id or uuid4().hex
    started = perf_counter()
    platform: str | None = None
    log_mcp_event(
        "mcp_extraction_started",
        request_id=request_id,
        platform=platform,
        outcome="started",
        duration_ms=0.0,
    )
    outcome, reason = "failed", "extraction_failed"
    item_count: int | None = None
    try:
        try:
            payload = McpExtractionInput.model_validate(arguments)
        except ValidationError:
            reason = "invalid_request"
            return error_result(reason)
        platform = platform_for_url(payload.url)
        result = to_mcp_result(await coordinator.execute(payload.url, "mcp"))
        public = result.model_dump(mode="json")
        response = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(public, ensure_ascii=False))],
            structured_content=public,
        )
        item_count = len(result.media)
        platform = result.platform
        outcome = "success"
        return response
    except asyncio.CancelledError:
        outcome, reason = "aborted", "cancelled"
        raise
    except AppError as error:
        reason = error.code if error.code in _PUBLIC_MESSAGES else "extraction_failed"
        return error_result(reason)
    except Exception:
        return error_result("extraction_failed")
    finally:
        event = {
            "success": "mcp_extraction_complete",
            "failed": "mcp_extraction_failed",
            "aborted": "mcp_extraction_aborted",
        }[outcome]
        log_mcp_event(
            event,
            request_id=request_id,
            platform=platform,
            outcome=outcome,
            duration_ms=(perf_counter() - started) * 1000,
            item_count=item_count,
            reason_code=reason,
        )
