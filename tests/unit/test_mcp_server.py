"""SDK factory 副作用與明確 transport 安全設定的驗證。"""

import asyncio
import logging

import httpx
import pytest
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from sns_media_list.api.limits import RequestLimiter
from sns_media_list.config import Settings
from sns_media_list.services.extraction_coordinator import ExtractionCoordinator
from tests.mcp_helpers import make_service


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
