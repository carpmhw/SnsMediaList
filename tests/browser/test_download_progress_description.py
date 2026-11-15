"""驗證進度條與可見下載詳情的單一可存取描述來源。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_folder_downloads as folder
from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_result_group_navigation import open_groups

page = workflow.page
engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("length", ["4", None])
def test_receiving_progress_describes_visible_transfer_detail(
    page: Page, base_url: str, length: str | None
) -> None:
    """資料夾替身等待write時，進度的描述與目前可見bytes詳情一致。"""
    folder.install_sink(page, hold="write")
    folder.prepare(page, base_url, count=1)
    folder.controlled_stream(page, length=length)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    progress = page.get_by_role("progressbar", name="目前檔案傳輸進度")
    detail = page.locator(".download-detail")
    identity = detail.get_attribute("id")
    assert identity
    expect(progress).to_have_attribute("aria-describedby", identity)
    text = detail.inner_text()
    assert "第 1 / 1 個檔案" in text and "2 bytes" in text
    expect(progress).to_have_accessible_description(text)
    if length:
        expect(progress).to_have_attribute("value", "50")
        assert "50%" in text
    else:
        assert progress.get_attribute("value") is None
        assert "%" not in text
    assert progress.get_attribute("aria-live") is None
    assert progress.get_attribute("tabindex") is None
    page.evaluate("/** 完成磁碟邊界替身，讓真實前端清理。 */ () => window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
    expect(progress).to_be_hidden()
    expect(detail).to_have_text("")


def test_progress_description_tracks_close_pending_state(page: Page, base_url: str) -> None:
    """傳輸已收齊仍須描述正在完成儲存，不能在close成功前宣稱已儲存。"""
    folder.install_sink(page, hold="close")
    folder.prepare(page, base_url, count=1)
    page.get_by_role("button", name="下載選取項目").click()
    detail = page.locator(".download-detail")
    expect(detail).to_have_text("正在完成儲存...")
    progress = page.get_by_role("progressbar", name="目前檔案傳輸進度")
    expect(progress).to_have_accessible_description("正在完成儲存...")
    expect(page.locator(".download-summary")).not_to_contain_text("已儲存 1")
    page.evaluate("/** 允許close真正完成後才計為已儲存。 */ () => window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
    expect(progress).to_be_hidden()


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_progress_description_ids_are_local_and_survive_result_replacement(
    engine_page: Page, base_url: str
) -> None:
    """批次進度各自關聯詳情，重新分析後不保留舊的描述參照。"""
    page = engine_page
    open_groups(page, base_url, 2)
    roots = page.locator(".result-group")
    identities: list[str] = []
    for index in range(2):
        detail = roots.nth(index).locator(".download-detail")
        identity = detail.get_attribute("id")
        assert identity
        identities.append(identity)
        expect(roots.nth(index).locator(".download-progress")).to_have_attribute(
            "aria-describedby", identity
        )
    assert len(set(identities)) == 2
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(roots).to_have_count(1)
    identity = page.locator(".download-detail").get_attribute("id")
    assert identity and identity not in identities
    expect(page.locator(".download-progress")).to_have_attribute("aria-describedby", identity)
