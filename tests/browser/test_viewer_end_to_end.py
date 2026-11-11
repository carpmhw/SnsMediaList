"""以前端到獨立正式應用及受控來源驗證完整檢視體驗。"""

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_playwright_workflow as workflow
from tests.integration.test_download_server import running_server

page = workflow.page


@pytest.mark.parametrize("kind", ["image", "video"])
def test_viewer_real_app_playback_and_expiration(page: Page, tmp_path: Path, kind: str) -> None:
    """真實 API／token／Range／UI 配合可解碼媒體，失效後明確復原。"""
    with running_server(tmp_path, kind=kind, viewer_assets=True) as origin:
        page.goto(origin)
        page.fill("#post-url", "https://www.instagram.com/p/fixture/")
        page.click("#analyze-button")
        expect(page.locator(".media-card")).to_have_count(1)
        page.locator(".media-selection").check()
        page.locator(".media-view").click()
        if kind == "image":
            page.wait_for_function(
                "document.querySelector('#viewer-content img')?.naturalWidth === 1600"
            )
        else:
            expect(page.locator("#viewer-play")).to_be_enabled()
            assert httpx.get(origin + "/fixture-stats").json()["fetches"] == 0
            page.click("#viewer-play")
            page.wait_for_function(
                "document.querySelector('#viewer-content video')?.currentTime > 0.15"
            )
            page.locator("#viewer-content video").evaluate(
                "node => { node.pause(); node.currentTime = 2; }"
            )
            page.wait_for_function("!document.querySelector('#viewer-content video').seeking")
            assert (
                abs(page.locator("#viewer-content video").evaluate("node => node.currentTime") - 2)
                < 0.1
            )
        page.click("#viewer-close")
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats["active"] == 0
        assert stats["closes"] == stats["fetches"] >= 1
        if kind == "video":
            assert any(
                isinstance(value, str) and value.startswith("bytes=") for value in stats["ranges"]
            )
        assert httpx.post(origin + "/fixture-expire").status_code == 204
        page.locator(".media-view").click()
        expect(page.locator("#viewer-recovery")).to_be_visible()
        assert httpx.get(origin + "/fixture-stats").json()["fetches"] == stats["fetches"]
        page.click("#viewer-recovery")
        expect(page.locator("#media-viewer")).not_to_be_visible()
        expect(page.locator(".media-selection")).not_to_be_checked()
        page.locator(".media-view").click()
        expect(page.locator("#viewer-message")).not_to_contain_text("參照已不可用")
        page.click("#viewer-close")
