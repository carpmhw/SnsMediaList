"""SDK factory 副作用與明確 transport 安全設定的驗證。"""

import asyncio
import logging

import httpx
import pytest
from mcp import Client
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from sns_media_list.api.limits import RequestLimiter
from sns_media_list.config import Settings
from sns_media_list.services.extraction_coordinator import ExtractionCoordinator
from tests.mcp_helpers import X_URL, make_service


@pytest.mark.parametrize("first_origin", [None, "http://192.168.50.14:8000"])
async def test_server_public_origins_are_app_local(first_origin: str | None) -> None:
    """交錯呼叫不同 server 時，每個 mapper 僅接收自己 Settings 的 origin。"""
    from sns_media_list.mcp.server import create_mcp_server

    servers = []
    for origin in (first_origin, "https://second.example"):
        settings = Settings(public_base_url=origin)
        service, _ = make_service(settings=settings)
        servers.append(
            create_mcp_server(
                ExtractionCoordinator(
                    service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)
                ),
                settings=settings,
            )
        )
    async with (
        Client(servers[0], cache=None) as first,
        Client(servers[1], cache=None) as second,
    ):
        await first.call_tool("extract_media", {"url": X_URL})
        second_result = await second.call_tool("extract_media", {"url": X_URL})
        first_result = await first.call_tool("extract_media", {"url": X_URL})
    for result, origin in (
        (first_result, first_origin),
        (second_result, "https://second.example"),
    ):
        assert result.is_error is False
        item = result.structured_content["media"][0]
        assert item["preview_url"].startswith(f"{origin or ''}/api/media/")
        assert item["download_url"].startswith(f"{origin or ''}/api/media/")


def test_factory_does_not_bind_or_change_root_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """建立 factory 不 bind port、不建立 task，也不改全域 logging。"""
    from sns_media_list.mcp.server import create_mcp_server

    def forbidden(*_args: object, **_kwargs: object) -> None:
        """在 factory 意外產生執行副作用時立即失敗。"""
        raise AssertionError("factory must not start resources")

    root = logging.getLogger()
    original = (root.level, root.handlers[:])
    monkeypatch.setattr(asyncio, "start_server", forbidden)
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    service, _ = make_service()
    server = create_mcp_server(
        ExtractionCoordinator(service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)),
        settings=Settings(),
    )
    assert server.name == "SNS Media List"
    assert (root.level, root.handlers) == original
    with pytest.raises(RuntimeError, match="streamable_http_app"):
        _ = server.session_manager


@pytest.mark.parametrize(
    ("host", "origin", "status"),
    [
        ("mcp.example:8443", None, 200),
        ("mcp.example:8443", "https://mcp.example:8443", 200),
        ("evil.example:8443", None, 421),
        ("mcp.example:8443", "https://evil.example", 403),
    ],
)
async def test_exact_transport_allowlists(host: str, origin: str | None, status: int) -> None:
    """SDK 原生 DNS-rebinding 防護只接受 operator 配置的精確 Host／Origin。"""
    from sns_media_list.mcp.server import create_mcp_http_app, create_mcp_server

    settings = Settings(
        mcp_allowed_hosts=("mcp.example:8443",), mcp_allowed_origins=("https://mcp.example:8443",)
    )
    service, _ = make_service(settings=settings)
    server = create_mcp_server(
        ExtractionCoordinator(service, limiter=RequestLimiter(max_extractions=1, max_downloads=1)),
        settings=settings,
    )
    app = create_mcp_http_app(server, settings=settings)
    headers = {
        "Host": host,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": "tools/list",
    }
    if origin:
        headers["Origin"] = origin
    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: "2026-07-28",
                            CLIENT_CAPABILITIES_META_KEY: {},
                        }
                    },
                },
            )
    assert response.status_code == status, response.text
