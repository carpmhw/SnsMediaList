"""驗證分析模式的命名群組、面板關聯及原生鍵盤切換。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_analysis_mode_group_preserves_panel_state_and_native_keyboard(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """命名group保留普通Tab、兩種啟動鍵、面板狀態、焦點與未提交輸入。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    requests: list[str] = []
    page.on(
        "request",
        lambda request: requests.append(request.url) if "/api/extractions" in request.url else None,
    )
    page.goto(base_url)
    modes = page.get_by_role("group", name="分析方式", exact=True)
    expect(modes).to_be_visible()
    single = modes.get_by_role("button", name="單筆分析", exact=True)
    batch = modes.get_by_role("button", name="批次分析", exact=True)
    expect(single).to_have_attribute("aria-controls", "single-input-panel")
    expect(batch).to_have_attribute("aria-controls", "batch-input-panel")
    expect(single).to_have_attribute("type", "button")
    expect(batch).to_have_attribute("type", "button")
    expect(single).to_have_attribute("aria-pressed", "true")
    expect(batch).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#single-input-panel")).to_be_visible()
    expect(page.locator("#batch-input-panel")).to_be_hidden()
    single_url = "https://x.com/creator/status/1"
    batch_urls = "https://x.com/creator/status/2\nhttps://x.com/creator/status/3"
    page.locator("#post-url").fill(single_url)
    single.focus()
    single.press("Tab")
    expect(batch).to_be_focused()
    batch.press(key)
    expect(batch).to_have_attribute("aria-pressed", "true")
    expect(single).to_have_attribute("aria-pressed", "false")
    expect(modes.locator('button[aria-pressed="true"]')).to_have_count(1)
    expect(page.locator("#single-input-panel")).to_be_hidden()
    expect(page.locator("#post-url")).to_be_disabled()
    expect(page.locator("#batch-input-panel")).to_be_visible()
    expect(page.locator("#batch-post-urls")).to_be_enabled()
    expect(page.locator("#batch-post-urls")).to_be_focused()
    page.locator("#batch-post-urls").fill(batch_urls)
    batch.focus()
    batch.press("Shift+Tab")
    expect(single).to_be_focused()
    single.press(key)
    expect(single).to_have_attribute("aria-pressed", "true")
    expect(batch).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#batch-input-panel")).to_be_hidden()
    expect(page.locator("#batch-post-urls")).to_be_disabled()
    expect(page.locator("#post-url")).to_be_enabled()
    expect(page.locator("#post-url")).to_be_focused()
    expect(page.locator("#post-url")).to_have_value(single_url)
    expect(page.locator("#batch-post-urls")).to_have_value(batch_urls)
    assert requests == []
