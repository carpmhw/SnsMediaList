"""Instagram 圖片與影片經真實 Uvicorn 的原生瀏覽器下載。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from tests.integration.download_server import IMAGE, SENTINEL, VIDEO
from tests.integration.test_download_server import events, running_server


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize(
    ("mode", "known"),
    [("success", True), ("success", False), ("error", True), ("error", False), ("truncated", True)],
    ids=["success-length", "success-chunked", "error-length", "error-chunked", "truncated"],
)
async def test_instagram_native_download(tmp_path: Path, kind: str, mode: str, known: bool) -> None:
    """走正式 UI 的 HEAD/GET，等待落盤或失敗，不把 download event 當成功。"""
    with running_server(tmp_path, kind=kind, mode=mode, known=known) as origin:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(accept_downloads=True)
                page.set_default_timeout(10000)
                await page.goto(origin)
                await page.fill("#post-url", "https://www.instagram.com/p/fixture/")
                async with page.expect_response("**/api/extractions") as extraction:
                    await page.click("#analyze-button")
                payload = await (await extraction.value).json()
                filename = payload["media"][0]["filename"]
                async with page.expect_download() as pending:
                    await page.get_by_role("button", name="下載", exact=True).click()
                download = await pending.value
                failure = await asyncio.wait_for(download.failure(), timeout=10)
                assert download.suggested_filename == filename
                if mode == "success":
                    assert failure is None
                    path = tmp_path / filename
                    await asyncio.wait_for(download.save_as(path), timeout=10)
                    expected = IMAGE if kind == "image" else VIDEO
                    assert path.stat().st_size == len(expected)
                    assert path.read_bytes() == expected
                else:
                    assert failure
                assert "已完成" not in await page.locator("#status").inner_text()
            finally:
                await browser.close()
        stats = httpx.get(origin + "/fixture-stats").json()
        attempts = stats["fetches"]
        # Chromium 會自行重試已知長度的失敗下載；每個 GET 是獨立 server lifecycle。
        assert 1 <= attempts <= 10
        if mode == "success" or not known:
            assert attempts == 1
        assert stats == {
            "fetches": attempts,
            "closes": attempts,
            "ranges": [None] * attempts,
            "active": 0,
            "methods": ["HEAD"] + ["GET"] * attempts,
        }
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    request_ids = {record["request_id"] for record in records}
    assert len(request_ids) == attempts
    for request_id in request_ids:
        lifecycle = [record for record in records if record["request_id"] == request_id]
        assert [event["event"] for event in lifecycle] == [
            "media_download_started",
            "media_download_completed" if mode == "success" else "media_download_failed",
        ]
        assert lifecycle[1]["platform"] == "instagram"
        assert lifecycle[1]["media_class"] == kind
    assert SENTINEL not in (tmp_path / "server.log").read_text()
