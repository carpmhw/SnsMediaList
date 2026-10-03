"""獨立 Uvicorn stderr 的 MCP 協定與敏感值去識別驗證。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import Client

from tests.integration.test_extraction_diagnostics import _stop_uvicorn

ROOT = Path(__file__).parents[2]


def start_server(public_base_url: str | None = None) -> tuple[subprocess.Popen[str], str]:
    """以動態 loopback port 與指定 public origin 啟動正式 logging 的替身 app。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "src")))
    environment.pop("SNS_MEDIA_INSTAGRAM_COOKIE_FILE", None)
    environment.pop("SNS_MEDIA_X_COOKIE_FILE", None)
    environment.pop("SNS_MEDIA_PUBLIC_BASE_URL", None)
    if public_base_url is not None:
        environment["SNS_MEDIA_PUBLIC_BASE_URL"] = public_base_url
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.integration._mcp_app:create_test_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-access-log",
            "--log-level",
            "warning",
            "--ws",
            "none",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(_stop_uvicorn(process))
        try:
            if httpx.get(f"{origin}/healthz", timeout=0.2).status_code == 200:
                return process, origin
        except httpx.TransportError:
            pass
        time.sleep(0.02)
    raise AssertionError("server startup timed out: " + _stop_uvicorn(process))


@pytest.mark.parametrize("base", [None, "https://SENSITIVE-BASE.example"])
async def test_subprocess_sdk_and_application_logs_do_not_leak(base: str | None) -> None:
    """有無 public origin 的成功、錯誤與取消均不輸出 URL、token 或敏感日誌。"""
    process, origin = start_server(base)
    urls = [f"https://x.com/SENSITIVE_USER/status/{index}/" for index in range(1, 5)]
    media_links: list[str] = []
    try:
        async with Client(f"{origin}/mcp", mode="2026-07-28", cache=None) as mcp:
            assert [tool.name for tool in (await mcp.list_tools()).tools] == ["extract_media"]
            for url, code in zip(
                urls[:3], (None, "post_unavailable", "extraction_failed"), strict=True
            ):
                result = await mcp.call_tool("extract_media", {"url": url})
                if code:
                    assert json.loads(result.content[0].text)["code"] == code
                    assert "SENSITIVE" not in result.model_dump_json()
                else:
                    assert result.is_error is False
                    item = result.structured_content["media"][0]
                    media_links.extend((item["preview_url"], item["download_url"]))
                    assert all(link.startswith(f"{base or ''}/api/media/") for link in media_links)
            invalid = await mcp.call_tool(
                "extract_media", {"url": urls[0], "SENSITIVE_COOKIE": "secret"}
            )
            assert "SENSITIVE" not in invalid.model_dump_json()
            task = asyncio.create_task(mcp.call_tool("extract_media", {"url": urls[3]}))
            async with httpx.AsyncClient(base_url=origin) as http:
                async with asyncio.timeout(5):
                    while not (await http.get("/_test/extraction_started")).json()["started"]:
                        await asyncio.sleep(0.02)
                # REST 的即時 429 證明 blocking MCP 已取得共享 slot。
                assert (
                    await http.post("/api/extractions", json={"url": urls[0]})
                ).status_code == 429
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                async with asyncio.timeout(5):
                    while (
                        await http.post("/api/extractions", json={"url": urls[0]})
                    ).status_code == 429:
                        await asyncio.sleep(0.02)
                malformed = await http.post(
                    "/mcp",
                    content=b"SENSITIVE_MALFORMED",
                    headers={"Content-Type": "application/json"},
                )
                assert malformed.status_code == 400
                assert "SENSITIVE" not in malformed.text
                for headers, expected in [
                    ({"Host": "SENSITIVE_HOST"}, 421),
                    ({"Origin": "https://SENSITIVE_ORIGIN"}, 403),
                ]:
                    rejected = await http.post(
                        "/mcp",
                        headers={"Accept": "application/json, text/event-stream", **headers},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    )
                    assert rejected.status_code == expected
                    assert "SENSITIVE" not in rejected.text
    finally:
        output = _stop_uvicorn(process)
    assert "SENSITIVE" not in output
    assert "FAKE_PRIVATE_SOURCE_SENTINEL" not in output
    assert "Traceback" not in output
    assert all(link not in output for link in media_links)
    assert all(urlsplit(link).path.split("/")[3] not in output for link in media_links)
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    names = {item["event"] for item in events}
    assert {
        "mcp_server_started",
        "mcp_extraction_complete",
        "mcp_extraction_failed",
        "mcp_extraction_aborted",
    } <= names
    for started in [item for item in events if item["event"] == "mcp_extraction_started"]:
        terminal = [
            item
            for item in events
            if item["request_id"] == started["request_id"]
            and item["event"]
            in {"mcp_extraction_complete", "mcp_extraction_failed", "mcp_extraction_aborted"}
        ]
        assert len(terminal) == 1
