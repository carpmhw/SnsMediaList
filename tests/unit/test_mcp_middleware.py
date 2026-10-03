"""MCP pre-parse body／attempt／response 邊界的 pure ASGI 測試。"""

import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from sns_media_list.api.limits import AttemptLimiter

if TYPE_CHECKING:
    from sns_media_list.mcp.middleware import McpRequestRegistry

VALID_BODY = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'


async def invoke_boundary(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[Message] | None = None,
    limit: int = 65536,
    attempts: AttemptLimiter | None = None,
    method: str = "POST",
    request_registry: "McpRequestRegistry | None" = None,
) -> tuple[list[Message], int, int]:
    """提供可計數 receive，確認早期拒絕不讀取或 dispatch。"""
    from sns_media_list.mcp.middleware import McpBoundaryMiddleware

    messages: list[Message] = []
    expected_body = (chunks or [{"type": "http.request", "body": VALID_BODY}])[0].get("body", b"")
    remaining = deque(chunks or [{"type": "http.request", "body": VALID_BODY}])
    reads, calls = 0, 0

    async def receive() -> Message:
        """依序回傳 request chunks，耗盡後視為 client disconnect。"""
        nonlocal reads
        reads += 1
        return remaining.popleft() if remaining else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        """擷取真實 ASGI response messages。"""
        messages.append(message)

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        """確認 body 一次重播並保留後續 disconnect 與 SDK header。"""
        nonlocal calls
        calls += 1
        if method == "POST":
            assert (await receive())["body"] == expected_body
            assert (await receive())["type"] == "http.disconnect"
        await Response("ok", headers={"Mcp-Session-Id": "session"})(scope, receive, send)

    limiter = attempts or AttemptLimiter(
        extraction_limit=10,
        media_limit=10,
        window_seconds=60,
        max_identities=32,
        reserve_mcp_cancellation=True,
    )
    app = McpBoundaryMiddleware(
        downstream,
        body_limit_bytes=limit,
        attempt_limiter=limiter,
        request_registry=request_registry,
    )
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": "/mcp",
        "state": {},
        "headers": headers or [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
    }
    await app(scope, receive, send)
    return messages, reads, calls


async def test_declared_oversized_body_is_rejected_without_reading() -> None:
    """Content-Length 超限時不接收 body，也不執行 SDK。"""
    messages, reads, calls = await invoke_boundary(
        headers=[(b"content-type", b"application/json"), (b"content-length", b"65537")]
    )
    assert messages[0]["status"] == 413
    assert reads == calls == 0


async def test_chunked_body_stops_consumption_as_soon_as_limit_is_crossed() -> None:
    """遇到超限 chunk 後不讀第三個 chunk，memory 與工作量有界。"""
    messages, reads, calls = await invoke_boundary(
        limit=8,
        chunks=[
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": True},
            {"type": "http.request", "body": b"must-not-read", "more_body": False},
        ],
    )
    assert messages[0]["status"] == 413
    assert reads == 2
    assert calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-type", b"text/plain")],
        [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")],
    ],
)
async def test_unsupported_representation_is_rejected_early(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """不在 MCP boundary 解壓縮或處理 form body。"""
    messages, reads, calls = await invoke_boundary(headers=headers)
    assert messages[0]["status"] == 415
    assert reads == calls == 0


async def test_valid_body_is_replayed_and_security_headers_preserve_sdk_headers() -> None:
    """重播後保留 disconnect，安全 headers 不覆蓋 SDK session header。"""
    messages, reads, calls = await invoke_boundary(
        headers=[
            (b"content-type", b"application/json; charset=utf-8"),
            (b"x-request-id", b"SENSITIVE"),
        ]
    )
    assert messages[0]["status"] == 200
    assert reads == 2
    assert calls == 1
    headers = dict(messages[0]["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"mcp-session-id"] == b"session"
    assert len(headers[b"x-request-id"]) == 32
    assert headers[b"x-request-id"] != b"SENSITIVE"


async def test_attempt_rejection_precedes_parsing_and_does_not_double_count() -> None:
    """同一固定 identity 的下一個 POST 解析有界 body 後被拒絕，不 dispatch。"""
    limiter = AttemptLimiter(
        extraction_limit=2,
        media_limit=1,
        window_seconds=60,
        max_identities=1,
        reserve_mcp_cancellation=True,
    )
    first, _, _ = await invoke_boundary(attempts=limiter)
    second, reads, calls = await invoke_boundary(attempts=limiter)
    assert first[0]["status"] == 200
    assert second[0]["status"] == 429
    assert int(dict(second[0]["headers"])[b"retry-after"]) >= 1
    assert reads == 1
    assert calls == 0
    assert limiter.identity_count == 0


@pytest.mark.parametrize("body", [b"SENSITIVE", b'{"jsonrpc":"2.0","id":{},"method":"SENSITIVE"}'])
async def test_malformed_envelope_uses_sdk_safe_error_without_echo(body: bytes) -> None:
    """在 SDK model 預檢遮蔽 legacy transport 可能回顯的 validation input。"""
    messages, _, calls = await invoke_boundary(chunks=[{"type": "http.request", "body": body}])
    assert messages[0]["status"] == 400
    serialized = messages[1]["body"].decode()
    assert "SENSITIVE" not in serialized
    assert json.loads(serialized)["jsonrpc"] == "2.0"
    assert calls == 0


async def test_disconnect_before_complete_body_does_not_dispatch() -> None:
    """未完成 body 的 client disconnect 不觸發擷取或偽造錯誤 response。"""
    messages, _, calls = await invoke_boundary(chunks=[{"type": "http.disconnect"}])
    assert messages == []
    assert calls == 0


async def test_get_delete_and_expired_window_keep_post_accounting_bounded() -> None:
    """GET／DELETE 不消耗 POST budget，monotonic window 到期可再接受請求。"""
    now = [0.0]

    def clock() -> float:
        """提供不依賴 sleep 的限流 clock。"""
        return now[0]

    limiter = AttemptLimiter(
        extraction_limit=2,
        media_limit=1,
        window_seconds=60,
        max_identities=1,
        clock=clock,
        reserve_mcp_cancellation=True,
    )
    for method in ("GET", "DELETE", "POST"):
        messages, _, _ = await invoke_boundary(attempts=limiter, method=method)
        assert messages[0]["status"] == 200
    rejected, _, _ = await invoke_boundary(attempts=limiter)
    assert rejected[0]["status"] == 429
    now[0] = 61.0
    admitted, _, _ = await invoke_boundary(attempts=limiter)
    assert admitted[0]["status"] == 200


async def test_active_legacy_cancellation_uses_reserved_slot_after_regular_quota() -> None:
    """一般 MCP POST quota 已滿時，active legacy cancellation 仍可使用保留額度。"""
    from sns_media_list.mcp.middleware import McpRequestRegistry

    registry = McpRequestRegistry()
    assert registry.begin("session-1", 7) is True
    attempts = AttemptLimiter(
        extraction_limit=3,
        media_limit=1,
        window_seconds=60,
        max_identities=1,
        reserve_mcp_cancellation=True,
    )
    ordinary = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    cancellation = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 7},
    }
    for message in (ordinary, ordinary):
        body = json.dumps(message).encode()
        messages, _reads, calls = await invoke_boundary(
            chunks=[{"type": "http.request", "body": body}],
            attempts=attempts,
            request_registry=registry,
            headers=[
                (b"content-type", b"application/json"),
                (b"mcp-session-id", b"session-1"),
            ],
        )
        assert messages[0]["status"] == 200
        assert calls == 1
    body = json.dumps(cancellation).encode()
    messages, _reads, calls = await invoke_boundary(
        chunks=[{"type": "http.request", "body": body}],
        attempts=attempts,
        request_registry=registry,
        headers=[
            (b"content-type", b"application/json"),
            (b"mcp-session-id", b"session-1"),
        ],
    )
    assert messages[0]["status"] == 200
    assert calls == 1
    body = json.dumps(ordinary).encode()
    messages, _reads, calls = await invoke_boundary(
        chunks=[{"type": "http.request", "body": body}],
        attempts=attempts,
        request_registry=registry,
        headers=[
            (b"content-type", b"application/json"),
            (b"mcp-session-id", b"session-1"),
        ],
    )
    assert messages[0]["status"] == 429
    assert calls == 0


def test_registry_matches_sdk_request_id_normalization_and_keeps_session_binding() -> None:
    """SDK 將數字字串 ID 與整數等同，registry 必須使用相同 correlation 規則。"""
    from sns_media_list.mcp.middleware import McpRequestRegistry

    registry = McpRequestRegistry()
    assert registry.begin("session-1", 7)
    assert registry.contains("session-1", "7")
    assert registry.contains("session-1", "+007")
    assert not registry.contains("session-2", "7")
    assert not registry.contains("session-1", True)
    registry.finish("session-1", "7")
    assert registry.active_count == 0
