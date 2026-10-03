"""MCP 正式路由、app-local factory 與 parent lifespan 的契約測試。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from starlette.applications import Starlette

from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.network.connect_proxy import ConnectProxy
from sns_media_list.services.extraction_coordinator import ExtractionCoordinator
from tests.mcp_helpers import X_URL, make_service

if TYPE_CHECKING:
    from sns_media_list.mcp.server import McpRuntime


class FakeProxy:
    """不 bind port 的 proxy 替身，記錄 resource 清理順序。"""

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        """保存 trace 與可注入的 startup failure。"""
        self.events = events
        self.fail = fail

    async def serve(self, _host: str, _port: int) -> asyncio.Server:
        """模擬 listener 啟動，回傳最小 server lifecycle 介面。"""
        self.events.append("proxy_start")
        if self.fail:
            raise RuntimeError("synthetic proxy failure")
        return cast(asyncio.Server, self)

    def close(self) -> None:
        """記錄 listener 已被關閉。"""
        self.events.append("proxy_close")

    async def wait_closed(self) -> None:
        """記錄等待 listener 結束。"""
        self.events.append("proxy_wait_closed")

    async def close_clients(self) -> None:
        """記錄 client connections 被清理。"""
        self.events.append("proxy_close_clients")


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("base", ["http://192.168.50.14:8000", "https://public.example"])
async def test_public_origin_keeps_rest_and_static_contracts(enabled: bool, base: str) -> None:
    """公開 origin 不改 REST links、錯誤、UI 或純 liveness，亦不探測外部位址。"""
    settings = Settings(mcp_enabled=enabled, public_base_url=base)
    service, extractor = make_service(settings=settings)
    app = create_app(settings=settings, extraction_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/app.js")).status_code == 200
        assert (await client.get("/placeholder.svg")).status_code == 200
        assert extractor.calls == 0
        response = await client.post("/api/extractions", json={"url": X_URL})
        assert response.status_code == 200
        public = response.json()
        assert set(public) == {
            "platform",
            "post_url",
            "author",
            "description",
            "unavailable_media_count",
            "media",
        }
        item = public["media"][0]
        assert item["preview_url"].startswith("/api/media/")
        assert item["preview_url"].endswith("/preview")
        assert item["download_url"].startswith("/api/media/")
        assert item["download_url"].endswith("/download")
        assert base not in response.text
        rejected = await client.post("/api/extractions", json={"url": "https://localhost/"})
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "unsupported_url"
    assert extractor.calls == 1


async def test_disabled_mcp_does_not_invoke_factory_or_hide_ui() -> None:
    """預設部署不建立 MCP，且 UI／health／REST 照常工作。"""

    def forbidden(_coordinator: ExtractionCoordinator, _settings: Settings) -> "McpRuntime":
        """若 disabled app 呼叫 MCP factory，立即使測試失敗。"""
        raise AssertionError("disabled factory must not be called")

    service, _ = make_service()
    app = create_app(settings=Settings(), extraction_service=service, mcp_factory=forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        assert (await client.post("/mcp")).status_code == 404
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/app.js")).status_code == 200
        assert (await client.post("/api/extractions", json={"url": X_URL})).status_code == 200


async def test_enabled_mcp_has_exact_endpoint_and_keeps_other_routes() -> None:
    """正式 /mcp 無 slash redirect，錯誤 double-prefix 不匹配 SDK route。"""
    events: list[str] = []
    settings = Settings(mcp_enabled=True)
    service, _ = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy(events)),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert '"serverInfo"' in response.text
            assert '"name":"SNS Media List"' in response.text
            assert "mcp-session-id" in response.headers
            assert response.headers["cache-control"] == "no-store"
            assert (await client.post("/mcp/mcp", json={})).status_code == 404
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert events[-3:] == ["proxy_close", "proxy_wait_closed", "proxy_close_clients"]


@pytest.mark.parametrize("failure", [None, "proxy", "manager_enter", "manager_exit", "cancel"])
async def test_parent_lifespan_owns_cleanup_even_on_failure(failure: str | None) -> None:
    """失敗或取消仍依 manager→proxy 順序清理，且 manager 不被重複使用。"""
    from sns_media_list.mcp.server import McpRuntime

    events: list[str] = []
    runtimes: list[McpRuntime] = []

    def factory(_coordinator: ExtractionCoordinator, _settings: Settings) -> McpRuntime:
        """每次 app construction 建立新 manager 替身。"""

        @asynccontextmanager
        async def run() -> AsyncIterator[None]:
            """模擬 manager context entry／exit 與明確失敗。"""
            events.append("manager_start")
            if failure == "manager_enter":
                raise RuntimeError("synthetic manager entry failure")
            try:
                yield
            finally:
                events.append("manager_stop")
                if failure == "manager_exit":
                    raise RuntimeError("synthetic manager exit failure")

        runtime = McpRuntime(http_app=Starlette(), run=run)
        runtimes.append(runtime)
        return runtime

    async def lifecycle() -> None:
        """進入 parent lifespan 並依測試條件取消 body。"""
        service, _ = make_service()
        app = create_app(
            settings=Settings(mcp_enabled=True),
            extraction_service=service,
            extraction_proxy=cast(ConnectProxy, FakeProxy(events, fail=failure == "proxy")),
            mcp_factory=factory,
        )
        async with app.router.lifespan_context(app):
            events.append("body")
            if failure == "cancel":
                raise asyncio.CancelledError()

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await lifecycle()
    elif failure:
        with pytest.raises(RuntimeError):
            await lifecycle()
    else:
        await lifecycle()
        await lifecycle()
        assert runtimes[0] is not runtimes[1]
    if failure == "proxy":
        assert "manager_start" not in events
    else:
        assert events[-3:] == ["proxy_close", "proxy_wait_closed", "proxy_close_clients"]
        if failure != "manager_enter":
            assert events.index("manager_stop") < events.index("proxy_close")


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Content-Type": "application/json"}, b"x" * 65537, 413),
        ({"Content-Type": "text/plain"}, b"SENSITIVE", 415),
        ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"SENSITIVE", 415),
        ({"Content-Type": "application/json"}, b"SENSITIVE", 400),
        ({"Content-Type": "application/json"}, b'{"jsonrpc":"bad","method":"SENSITIVE"}', 400),
    ],
)
async def test_parent_mcp_boundary_rejects_before_sdk_start(
    headers: dict[str, str],
    body: bytes,
    status: int,
) -> None:
    """未啟動 lifespan 也可在 SDK parsing 前拒絕超限／無效 representation。"""
    service, extractor = make_service()
    app = create_app(settings=Settings(mcp_enabled=True), extraction_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        response = await client.post("/mcp", headers=headers, content=body)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "SENSITIVE" not in response.text
    assert extractor.calls == 0
