"""真實本機 Streamable HTTP、token API 與 REST／MCP 共享 budget 測試。"""

import asyncio
import json
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import Client
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.network.connect_proxy import ConnectProxy
from sns_media_list.network.media_client import MediaClient
from tests.api.test_mcp import FakeProxy
from tests.mcp_helpers import (
    PRIVATE_SOURCE,
    X_URL,
    FakeExtractor,
    FakeMediaClient,
    make_service,
    running_app,
)


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("base", [None, "http://192.168.50.14:8000", "https://public.example"])
async def test_official_http_client_and_existing_token_api(mode: str, base: str | None) -> None:
    """SDK 兩個協定世代回傳相對／絕對連結，token path 仍可在本機取得。"""
    now = [0.0]

    def clock() -> float:
        """提供 deterministic token 到期控制。"""
        return now[0]

    settings = Settings(mcp_enabled=True, public_base_url=base)
    service, extractor = make_service(settings=settings, clock=clock)
    media = FakeMediaClient()
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
        media_client=cast(MediaClient, media),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode=mode, cache=None) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["extract_media"]
            result = await client.call_tool("extract_media", {"url": X_URL})
        assert result.is_error is False
        assert PRIVATE_SOURCE not in result.model_dump_json()
        assert json.loads(result.content[0].text) == result.structured_content
        item = result.structured_content["media"][0]
        assert item["preview_url"].startswith(f"{base or ''}/api/media/")
        assert item["download_url"].startswith(f"{base or ''}/api/media/")
        preview_path = urlsplit(item["preview_url"]).path
        download_path = urlsplit(item["download_url"]).path
        async with httpx.AsyncClient(base_url=origin) as http:
            assert (await http.head(download_path)).status_code == 204
            download = await http.get(download_path)
            assert download.status_code == 200
            assert download.content == media.body
            assert "attachment" in download.headers["content-disposition"]
            assert (await http.get(preview_path)).content == media.body
            wrong_purpose = preview_path.replace("/preview", "/download")
            assert (await http.get(wrong_purpose)).status_code == 404
            now[0] = 601
            assert (await http.get(download_path)).status_code == 410
    assert extractor.calls == 1
    assert all(writer.closed for writer in media.writers)
    assert all("cookie" not in {key.lower() for key in headers} for headers in media.headers)


@pytest.mark.parametrize("base", [None, "https://public.example"])
async def test_mcp_token_capacity_failure_is_atomic(base: str | None) -> None:
    """有無公開 origin 都不會在容量不足時留下部分 MCP token。"""
    settings = Settings(mcp_enabled=True, public_base_url=base)
    service, _ = make_service(settings=settings, capacity=1)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode="2026-07-28", cache=None) as client:
            result = await client.call_tool("extract_media", {"url": X_URL})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == "capacity_exceeded"
    assert service.token_store.size == 0


async def test_absolute_placeholder_uses_existing_static_route() -> None:
    """缺少 poster 的影片取得絕對 placeholder，解析後仍使用既有 static route。"""
    settings = Settings(mcp_enabled=True, public_base_url="https://public.example")
    service, _ = make_service(
        settings=settings, extractor=FakeExtractor(media_types=("video",), preview=False)
    )
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", cache=None) as client:
            result = await client.call_tool("extract_media", {"url": X_URL})
        preview = result.structured_content["media"][0]["preview_url"]
        assert preview == "https://public.example/placeholder.svg"
        async with httpx.AsyncClient(base_url=origin) as http:
            response = await http.get(urlsplit(preview).path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize("base", [None, "https://public.example"])
async def test_forwarded_headers_do_not_determine_media_origin(base: str | None) -> None:
    """合法 localhost Host 下的任意 Forwarded headers 不改變 MCP 媒體 origin。"""
    settings = Settings(mcp_enabled=True, public_base_url=base)
    service, _ = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin) as http:
            response = await http.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/call",
                    "MCP-Name": "extract_media",
                    "X-Forwarded-Host": "other.example",
                    "X-Forwarded-Proto": "http",
                    "Forwarded": "host=forwarded.example;proto=http",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "extract_media",
                        "arguments": {"url": X_URL},
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: "2026-07-28",
                            CLIENT_CAPABILITIES_META_KEY: {},
                        },
                    },
                },
            )
    assert response.status_code == 200
    if response.headers["content-type"].startswith("text/event-stream"):
        payload = json.loads(
            next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        )
    else:
        payload = response.json()
    item = payload["result"]["structuredContent"]["media"][0]
    assert item["preview_url"].startswith(f"{base or ''}/api/media/")
    assert item["download_url"].startswith(f"{base or ''}/api/media/")


@pytest.mark.parametrize("first", ["rest", "mcp"])
async def test_rest_and_mcp_compete_for_one_shared_slot(first: str) -> None:
    """任何 transport 占用唯一 slot 時，另一個被即時拒絕且不啟動第二個 extractor。"""
    settings = Settings(mcp_enabled=True, max_extractions=1)
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode="2026-07-28", cache=None) as mcp:
            async with httpx.AsyncClient(base_url=origin) as rest:
                if first == "rest":
                    task = asyncio.create_task(rest.post("/api/extractions", json={"url": X_URL}))
                else:
                    task = asyncio.create_task(mcp.call_tool("extract_media", {"url": X_URL}))
                try:
                    async with asyncio.timeout(5):
                        await extractor.started.wait()
                    if first == "rest":
                        rejected = await mcp.call_tool("extract_media", {"url": X_URL})
                        assert json.loads(rejected.content[0].text)["code"] == "local_rate_limited"
                    else:
                        rejected_http = await rest.post("/api/extractions", json={"url": X_URL})
                        assert rejected_http.status_code == 429
                        assert rejected_http.json()["code"] == "local_rate_limited"
                        assert int(rejected_http.headers["retry-after"]) >= 1
                    assert extractor.calls == 1
                    assert extractor.maximum_active == 1
                finally:
                    extractor.release.set()
                    await task
                assert (await rest.post("/api/extractions", json={"url": X_URL})).status_code == 200


async def test_larger_budget_still_has_one_mcp_identity_and_global_limit() -> None:
    """不同 REST identities 可使用第二個 slot，但 MCP 不形成獨立 budget。"""
    settings = Settings(mcp_enabled=True, max_extractions=2, trusted_proxy_cidrs=("127.0.0.1/32",))
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode="2026-07-28", cache=None) as mcp:
            async with httpx.AsyncClient(base_url=origin) as rest:
                first = asyncio.create_task(mcp.call_tool("extract_media", {"url": X_URL}))
                second = None
                try:
                    async with asyncio.timeout(5):
                        await extractor.started.wait()
                    duplicate = await mcp.call_tool("extract_media", {"url": X_URL})
                    assert json.loads(duplicate.content[0].text)["code"] == "local_rate_limited"
                    second = asyncio.create_task(
                        rest.post(
                            "/api/extractions",
                            json={"url": X_URL},
                            headers={"X-Forwarded-For": "8.8.8.8"},
                        )
                    )
                    async with asyncio.timeout(5):
                        while extractor.active != 2:
                            await asyncio.sleep(0.01)
                    rejected = await rest.post(
                        "/api/extractions",
                        json={"url": X_URL},
                        headers={"X-Forwarded-For": "1.1.1.1"},
                    )
                    assert rejected.status_code == 429
                    assert extractor.maximum_active == 2
                finally:
                    extractor.release.set()
                    await first
                    if second is not None:
                        await second


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_real_transport_cancellation_releases_extraction_slot(mode: str) -> None:
    """SDK cancellation 經真實 HTTP 傳遞，取消工作後 REST 可重新取得 slot。"""
    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode=mode, cache=None) as mcp:
            task = asyncio.create_task(mcp.call_tool("extract_media", {"url": X_URL}))
            async with asyncio.timeout(5):
                await extractor.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(5):
                while extractor.active:
                    await asyncio.sleep(0.01)
            extractor.release.set()
            async with httpx.AsyncClient(base_url=origin) as rest:
                assert (await rest.post("/api/extractions", json={"url": X_URL})).status_code == 200


async def test_legacy_cancellation_reserve_works_when_regular_post_quota_is_full() -> None:
    """最後一個 active legacy extraction 可取消，不被一般 POST quota 擋下。"""
    settings = Settings(mcp_enabled=True, max_extractions=1)
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with Client(f"{origin}/mcp", mode="legacy", cache=None) as mcp:
            for _ in range(6):
                await mcp.list_tools()
            task = asyncio.create_task(mcp.call_tool("extract_media", {"url": X_URL}))
            async with asyncio.timeout(5):
                await extractor.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(5):
                while extractor.active:
                    await asyncio.sleep(0.01)
            extractor.release.set()
            async with httpx.AsyncClient(base_url=origin) as rest:
                response = await rest.post("/api/extractions", json={"url": X_URL})
                assert response.status_code == 200


async def test_non_object_rpc_arguments_keep_sdk_protocol_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RPC envelope 的錯誤保留 SDK -32602，不啟動 tool 或洩漏原始 arguments。"""
    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    headers = {"Accept": "application/json, text/event-stream"}
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin) as http:
            initialize = await http.post(
                "/mcp",
                headers=headers,
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
            session_id = initialize.headers["mcp-session-id"]
            session_headers = {**headers, "Mcp-Session-Id": session_id}
            await http.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            response = await http.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "extract_media", "arguments": "SENSITIVE"},
                },
            )
    assert response.status_code == 200
    assert '"code":-32602' in response.text
    assert "SENSITIVE" not in response.text
    assert extractor.calls == 0
    assert "mcp_extraction_started" not in capsys.readouterr().err


async def test_legacy_stringified_request_id_can_use_cancellation_reserve() -> None:
    """真實 HTTP 數字字串取消沿用 SDK correlation，且不能由其他 session 使用。"""
    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=5) as http:
            headers = {"Accept": "application/json, text/event-stream"}
            initialize = await http.post(
                "/mcp",
                headers=headers,
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
            session = initialize.headers["mcp-session-id"]
            headers["Mcp-Session-Id"] = session
            await http.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            async with http.stream(
                "POST",
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "extract_media", "arguments": {"url": X_URL}},
                },
            ) as response:
                assert response.status_code == 200
                async with asyncio.timeout(5):
                    await extractor.started.wait()
                for index in range(6):
                    assert (
                        await http.post(
                            "/mcp",
                            headers=headers,
                            json={
                                "jsonrpc": "2.0",
                                "id": 100 + index,
                                "method": "tools/list",
                            },
                        )
                    ).status_code == 200
                cancellation = {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "7"},
                }
                wrong = await http.post(
                    "/mcp",
                    headers={**headers, "Mcp-Session-Id": "other-session"},
                    json=cancellation,
                )
                assert wrong.status_code == 429
                assert extractor.active == 1
                correct = await http.post("/mcp", headers=headers, json=cancellation)
                assert correct.status_code == 202
                async with asyncio.timeout(5):
                    while extractor.active:
                        await asyncio.sleep(0.01)
            extractor.release.set()
            assert (await http.post("/api/extractions", json={"url": X_URL})).status_code == 200


async def test_real_http_identity_churn_cannot_reset_mcp_window() -> None:
    """真實 middleware 的 trusted identity churn 不會淘汰 MCP aggregate window。"""
    settings = Settings(
        mcp_enabled=True,
        rate_limit_identity_capacity=1,
        rate_limit_extraction_attempts=4,
        trusted_proxy_cidrs=("127.0.0.1/32",),
    )
    service, _ = make_service(settings=settings)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin) as http:
            for _ in range(3):
                assert (
                    await http.post(
                        "/mcp",
                        content=b"invalid-json",
                        headers={"Content-Type": "application/json"},
                    )
                ).status_code == 400
            for identity in ("8.8.8.8", "1.1.1.1", "9.9.9.9"):
                assert (
                    await http.post(
                        "/api/extractions",
                        json={"url": "https://unsupported.example/"},
                        headers={"X-Forwarded-For": identity},
                    )
                ).status_code == 400
                assert (
                    await http.get(
                        "/api/media/unknown/download", headers={"X-Forwarded-For": identity}
                    )
                ).status_code == 404
            response = await http.post(
                "/mcp", content=b"invalid-json", headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 429
            assert app.state.attempt_limiter.identity_count == 1


async def test_modern_request_cannot_forge_legacy_cancellation_registration() -> None:
    """Modern request 的自填 session header 不得產生 legacy cancellation correlation。"""
    settings = Settings(mcp_enabled=True)
    service, extractor = make_service(settings=settings, extractor=FakeExtractor(blocking=True))
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=5) as http:
            task = asyncio.create_task(
                http.post(
                    "/mcp",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "MCP-Method": "tools/call",
                        "MCP-Name": "extract_media",
                        "Mcp-Session-Id": "fabricated-session",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "extract_media",
                            "arguments": {"url": X_URL},
                            "_meta": {
                                PROTOCOL_VERSION_META_KEY: "2026-07-28",
                                CLIENT_CAPABILITIES_META_KEY: {},
                            },
                        },
                    },
                )
            )
            try:
                async with asyncio.timeout(5):
                    await extractor.started.wait()
                assert app.state.mcp_runtime.request_registry.active_count == 0
                cancelled = await http.post(
                    "/mcp",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Mcp-Session-Id": "fabricated-session",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": "7"},
                    },
                )
                assert cancelled.status_code == 404
                # Cancellation reserve 仍保留；此 SDK-rejected request 僅消耗一般 POST。
                app.state.attempt_limiter.acquire_mcp_post(cancellation=True)
                assert extractor.active == 1
            finally:
                extractor.release.set()
                response = await task
                assert response.status_code == 200
