"""官方 SDK typed server factory；不建立 listener 或重複核心相依。"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    Tool,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.applications import Starlette
from starlette.requests import Request

from ..config import Settings
from ..logging_config import configure_mcp_logging
from ..services.extraction_coordinator import ExtractionCoordinator
from .middleware import McpRequestRegistry
from .models import McpExtractionInput, McpExtractionResult
from .tools import error_result, execute_tool


@dataclass(frozen=True, slots=True)
class McpRuntime:
    """Parent app 擁有的 SDK HTTP app 與 manager context entry。"""

    http_app: Starlette
    run: Callable[[], AbstractAsyncContextManager[None]]
    request_registry: McpRequestRegistry | None = None


type McpRuntimeFactory = Callable[[ExtractionCoordinator, Settings], McpRuntime]


def create_mcp_server(
    coordinator: ExtractionCoordinator,
    *,
    settings: Settings,
    request_registry: McpRequestRegistry | None = None,
) -> Server[object]:
    """以 application-owned schema 註冊單一 tool，完整協定由官方 SDK 處理。"""
    configure_mcp_logging()
    tool = Tool(
        name="extract_media",
        description="分析一個支援的 Instagram／X 單篇 URL，回傳 metadata 與短效媒體連結。",
        input_schema=McpExtractionInput.model_json_schema(),
        output_schema=McpExtractionResult.model_json_schema(),
    )

    async def list_tools(
        _context: ServerRequestContext[object, object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        """固定回傳唯一第一階段 tool，不暴露額外能力。"""
        return ListToolsResult(tools=[tool])

    async def call_tool(
        context: ServerRequestContext[object, object],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        """交給安全 adapter 並傳入 app-local origin，僅採用服務端 correlation ID。"""
        if params.name != "extract_media":
            return error_result("invalid_request")
        request = context.request
        request_id = (
            getattr(request.state, "request_id", None) if isinstance(request, Request) else None
        )
        session_id = request.headers.get("mcp-session-id") if isinstance(request, Request) else None
        registered = (
            request_registry.begin(session_id, context.request_id)
            if request_registry is not None
            and context.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
            else False
        )
        try:
            return await execute_tool(
                coordinator,
                params.arguments or {},
                request_id=request_id,
                public_base_url=settings.public_base_url,
            )
        finally:
            if registered and request_registry is not None:
                request_registry.finish(session_id, context.request_id)

    def input_schema(name: str) -> Mapping[str, object] | None:
        """供 SDK 執行協定層 schema 檢查，不自行重寫 protocol dispatch。"""
        return tool.input_schema if name == "extract_media" else None

    server = Server[object](
        "SNS Media List",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=input_schema,
    )
    return server


def create_mcp_http_app(
    server: Server[object],
    *,
    settings: Settings,
) -> Starlette:
    """建立 SDK /mcp route 與有界 manager，不啟動其 lifespan 或 listener。"""
    security = None
    if settings.mcp_allowed_hosts or settings.mcp_allowed_origins:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.mcp_allowed_hosts),
            allowed_origins=list(settings.mcp_allowed_origins),
        )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        # SDK 純 JSON 模式不監聽現代協定 disconnect，保留預設回應模式的取消能力。
        json_response=False,
        max_request_body_size=settings.mcp_max_request_body_bytes,
        transport_security=security,
        max_sessions=32,
        session_idle_timeout=float(settings.token_ttl_seconds),
    )


def create_mcp_runtime(coordinator: ExtractionCoordinator, settings: Settings) -> McpRuntime:
    """每個 app 建立獨立 server／manager，factory 不啟動背景生命週期。"""
    registry = McpRequestRegistry()
    server = create_mcp_server(coordinator, settings=settings, request_registry=registry)
    http_app = create_mcp_http_app(server, settings=settings)
    return McpRuntime(http_app=http_app, run=server.session_manager.run, request_registry=registry)
