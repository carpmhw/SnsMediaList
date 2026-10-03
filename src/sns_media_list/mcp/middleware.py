"""MCP 專屬 pure ASGI pre-parse、attempt 與回應隱私邊界。"""

import hashlib
from uuid import uuid4

from mcp.shared.dispatcher import coerce_request_id
from mcp.types import (
    INVALID_REQUEST,
    PARSE_ERROR,
    CancelledNotificationParams,
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..api.limits import AttemptLimiter
from ..errors import AppError
from ..models import ErrorResponse

_MESSAGE_ADAPTER = TypeAdapter[JSONRPCMessage](JSONRPCMessage)


class McpRequestRegistry:
    """保留最多 32 個 active legacy extraction request 的不可逆識別。"""

    def __init__(self, *, max_active: int = 32) -> None:
        """建立 bounded registry，不保存原始 session 或 request ID。"""
        self.max_active = max_active
        self._active: dict[bytes, int] = {}

    @property
    def active_count(self) -> int:
        """回傳目前 active request 數量，供測試與 bounded lifecycle 使用。"""
        return len(self._active)

    def begin(self, session_id: str | None, request_id: object) -> bool:
        """註冊一個 session/request pair，超過容量時 fail closed。"""
        key = self._key(session_id, request_id)
        if key is None:
            return False
        if key not in self._active and len(self._active) >= self.max_active:
            return False
        self._active[key] = self._active.get(key, 0) + 1
        return True

    def finish(self, session_id: str | None, request_id: object) -> None:
        """移除一個完成、失敗或取消的 active request。"""
        key = self._key(session_id, request_id)
        if key is None or key not in self._active:
            return
        count = self._active[key] - 1
        if count > 0:
            self._active[key] = count
        else:
            del self._active[key]

    def contains(self, session_id: str | None, request_id: object) -> bool:
        """判斷 cancellation 是否精確匹配相同 session 的 active request。"""
        key = self._key(session_id, request_id)
        return key is not None and key in self._active

    @staticmethod
    def _key(session_id: str | None, request_id: object) -> bytes | None:
        """以固定 digest 比對 user-controlled correlation，避免保留敏感原文。"""
        if not isinstance(session_id, str) or not session_id:
            return None
        if type(request_id) is not int and type(request_id) is not str:
            return None
        assert isinstance(request_id, str | int)
        canonical = coerce_request_id(request_id)
        value = f"{type(canonical).__name__}:{canonical}"
        return hashlib.sha256(f"{session_id}\x00{value}".encode()).digest()


class McpBoundaryMiddleware:
    """在 SDK 消耗 body 前限制 POST，保留其餘協定與取消語意。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        body_limit_bytes: int,
        attempt_limiter: AttemptLimiter,
        request_registry: McpRequestRegistry | None = None,
    ) -> None:
        """注入限定路由與同一 app 的有界 attempt limiter。"""
        self.app = app
        self.body_limit_bytes = body_limit_bytes
        self.attempt_limiter = attempt_limiter
        self.request_registry = request_registry or McpRequestRegistry()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """處理 MCP HTTP request，非 HTTP scope 原樣通過。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def secured_send(message: Message) -> None:
            """強制 response privacy 並保留所有 SDK 協定 headers。"""
            if message["type"] == "http.response.start":
                message = dict(message)
                names = {b"cache-control", b"referrer-policy", b"x-request-id"}
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in names
                ]
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-request-id", request_id.encode("ascii")),
                    ]
                )
                message["headers"] = headers
            await send(message)

        if scope["method"] == "POST":
            session_id = Headers(scope=scope).get("mcp-session-id")
            try:
                body = await self._read_body(scope, receive)
            except AppError as error:
                response = JSONResponse(
                    status_code=error.status_code or 500,
                    content=ErrorResponse(
                        code=error.code, message=error.message, request_id=request_id
                    ).model_dump(),
                )
                if error.retry_after is not None:
                    response.headers["Retry-After"] = str(error.retry_after)
                await response(scope, receive, secured_send)
                return
            if body is None:
                return
            parsed_message: JSONRPCMessage | None
            parse_error: ValidationError | ValueError | RecursionError | None = None
            try:
                parsed_message = _MESSAGE_ADAPTER.validate_json(body, strict=True, extra="forbid")
            except (ValidationError, ValueError, RecursionError) as error:
                parsed_message = None
                parse_error = error
            is_cancellation = self._is_active_cancellation(
                parsed_message, session_id, Headers(scope=scope).get("mcp-protocol-version")
            )
            try:
                self.attempt_limiter.acquire_mcp_post(cancellation=is_cancellation)
            except AppError as error:
                response = JSONResponse(
                    status_code=error.status_code or 500,
                    content=ErrorResponse(
                        code=error.code, message=error.message, request_id=request_id
                    ).model_dump(),
                )
                if error.retry_after is not None:
                    response.headers["Retry-After"] = str(error.retry_after)
                await response(scope, receive, secured_send)
                return
            if parse_error is not None:
                payload = self._protocol_error(parse_error)
                await JSONResponse(
                    status_code=400, content=payload.model_dump(mode="json", by_alias=True)
                )(scope, receive, secured_send)
                return
            assert parsed_message is not None
            original_receive = receive
            delivered = False

            async def replay() -> Message:
                """只重播完整有界 body 一次，後續 receive 保留真正 disconnect。"""
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await original_receive()

            receive = replay
            await self.app(scope, receive, secured_send)
            return
        await self.app(scope, receive, secured_send)

    def _is_active_cancellation(
        self,
        message: JSONRPCMessage | None,
        session_id: str | None,
        protocol_version: str | None = None,
    ) -> bool:
        """只將同一 legacy session 的 active request cancellation 分到 reserve。"""
        if (
            not isinstance(message, JSONRPCNotification)
            or message.method != "notifications/cancelled"
        ):
            return False
        if protocol_version is not None and protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
            return False
        try:
            params = CancelledNotificationParams.model_validate(
                message.params, strict=True, extra="forbid"
            )
        except ValidationError:
            return False
        return self.request_registry.contains(session_id, params.request_id)

    @staticmethod
    def _protocol_error(
        error: ValidationError | ValueError | RecursionError,
    ) -> JSONRPCError:
        """建立不回顯 input 的固定 JSON-RPC parse／request error。"""
        code = INVALID_REQUEST
        if type(error) is ValidationError and any(
            item["type"] == "json_invalid"
            for item in error.errors(include_input=False, include_url=False)
        ):
            code = PARSE_ERROR
        return JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(
                code=code, message="Parse error" if code == PARSE_ERROR else "Invalid request"
            ),
        )

    async def _read_body(self, scope: Scope, receive: Receive) -> bytes | None:
        """驗證 representation、declared／streamed bytes；disconnect 不 dispatch。"""
        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        content_encoding = headers.get("content-encoding", "identity").strip().lower()
        if content_type != "application/json" or content_encoding != "identity":
            raise AppError("unsupported_media_type", "MCP requires an unencoded JSON body.")
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > self.body_limit_bytes:
            raise AppError("request_too_large", "The MCP request body is too large.")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.body_limit_bytes:
                raise AppError("request_too_large", "The MCP request body is too large.")
            body.extend(chunk)
            if not message.get("more_body", False):
                return bytes(body)
