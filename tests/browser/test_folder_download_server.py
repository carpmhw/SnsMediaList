"""真實 Uvicorn body 串流至可檢查 bytes 的 writable 替身；不代表真機 picker。"""

from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests.browser.test_folder_downloads import SINK_SETUP
from tests.integration.download_server import IMAGE, SENTINEL, VIDEO
from tests.integration.test_download_server import events, running_server


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("mode", ["success", "error"])
async def test_folder_receives_real_http_body(
    tmp_path: Path, kind: str, known: bool, mode: str
) -> None:
    """捕捉真實 framing 中斷被當成功，驗證完整 bytes、close 及 server 資源釋放。"""
    with running_server(tmp_path, kind=kind, known=known, mode=mode) as origin:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.set_default_timeout(10000)
                await page.add_init_script(f"({SINK_SETUP})({{}});")
                await page.goto(origin)
                await page.fill("#post-url", "https://www.instagram.com/p/fixture/")
                await page.click("#analyze-button")
                await page.locator(".media-selection").check()
                await page.get_by_role("button", name="下載選取項目").click()
                await expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
                files = await page.evaluate("Object.values(window.files)")
                if mode == "success":
                    await expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
                    assert len(files) == 1
                    assert files[0]["closed"] is True
                    assert bytes(files[0]["bytes"]) == (IMAGE if kind == "image" else VIDEO)
                else:
                    await expect(page.locator(".download-summary")).to_contain_text("失敗 1")
                    assert all(not file["closed"] and file["aborted"] for file in files)
                await page.wait_for_function(
                    """/** 等待正式 server 的下載 lease 已釋放。 */
                    async () => (await (await fetch('/fixture-stats')).json()).active === 0"""
                )
            finally:
                await browser.close()
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats == {
            "fetches": 1,
            "closes": 1,
            "ranges": [None],
            "active": 0,
            "methods": ["HEAD", "GET"],
        }
    records = [
        record for record in events(tmp_path) if record["event"].startswith("media_download")
    ]
    assert [record["event"] for record in records] == [
        "media_download_started",
        "media_download_completed" if mode == "success" else "media_download_failed",
    ]
    assert SENTINEL not in (tmp_path / "server.log").read_text()


@pytest.mark.parametrize("failure", ["cancel", "write", "close"])
async def test_folder_local_failure_releases_real_server(tmp_path: Path, failure: str) -> None:
    """捕捉 client 停止／磁碟失敗未取消 HTTP、提交殘檔或遺留 lease。"""
    mode = "disconnect" if failure == "cancel" else "success"
    with running_server(tmp_path, mode=mode) as origin:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.set_default_timeout(10000)
                options = "{}" if failure == "cancel" else f'{{"fail":"{failure}"}}'
                await page.add_init_script(f"({SINK_SETUP})({options});")
                await page.goto(origin)
                await page.fill("#post-url", "https://www.instagram.com/p/fixture/")
                await page.click("#analyze-button")
                await page.locator(".media-selection").check()
                await page.get_by_role("button", name="下載選取項目").click()
                if failure == "cancel":
                    await page.wait_for_function("window.diskEvents.includes('write')")
                    await page.get_by_role("button", name="停止下載").click()
                await expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
                await expect(page.locator(".download-summary")).to_contain_text(
                    "已停止" if failure == "cancel" else "失敗 1"
                )
                assert await page.evaluate(
                    "Object.values(window.files).every(file => file.aborted && !file.closed)"
                )
                await page.wait_for_function(
                    """/** 等待網路取消後 server 完成 cleanup。 */
                    async () => (await (await fetch('/fixture-stats')).json()).active === 0"""
                )
            finally:
                await browser.close()
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats["active"] == 0
        assert stats["fetches"] == stats["closes"] == 1
        assert stats["methods"] == ["HEAD", "GET"]
    records = [
        record for record in events(tmp_path) if record["event"].startswith("media_download")
    ]
    assert len(records) == 2
    assert records[0]["event"] == "media_download_started"
    assert records[1]["event"] in {"media_download_completed", "media_download_aborted"}
    if failure == "cancel":
        assert records[1]["event"] == "media_download_aborted"
    assert SENTINEL not in (tmp_path / "server.log").read_text()
