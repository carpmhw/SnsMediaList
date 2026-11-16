"""驗證媒體選取的原生語意、點擊區與群組本地狀態。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_result_group_navigation import open_groups

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
def test_selection_label_has_touch_target_and_native_name(
    engine_page: Page, base_url: str, width: int
) -> None:
    """點擊標籤邊緣與Space均使用真正checkbox，不需自訂鍵盤或名稱覆寫。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    viewer_workflow.open_results(page, base_url)
    label = page.locator(".media-selection-label").first
    checkbox = page.get_by_role("checkbox", name="選取項目 01 第 1 組", exact=True)
    bounds = label.bounding_box()
    assert bounds and bounds["height"] >= 44 and bounds["width"] >= 44
    assert checkbox.get_attribute("aria-label") is None
    assert checkbox.evaluate(
        "/** 名稱仍由原生label提供，含可見項目文字及輔助群組上下文。 */ el => "
        "el.labels.length === 1 && el.labels[0].textContent.trim() === '選取項目 01 第 1 組'"
    )
    label.click(position={"x": bounds["width"] - 3, "y": 3})
    expect(checkbox).to_be_checked()
    expect(page.locator(".selection-summary")).to_have_text("1 / 2 已選取")
    checkbox.focus()
    checkbox.press("Space")
    expect(checkbox).not_to_be_checked()
    expect(checkbox).to_be_focused()
    expect(page.locator(".selection-summary")).to_have_text("0 / 2 已選取")
    expect(page.locator("#media-viewer")).to_be_hidden()


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_named_selection_group_preserves_native_keyboard_actions(
    engine_page: Page, base_url: str
) -> None:
    """命名動作群組不冒充composite toolbar，Tab與一次性選取動作保持原生。"""
    page = engine_page
    viewer_workflow.open_results(page, base_url)
    group = page.get_by_role("group", name="媒體選取操作", exact=True)
    expect(group).to_be_visible()
    select_all = group.get_by_role("button", name="全選", exact=True)
    select_all.focus()
    select_all.press("Enter")
    expect(page.locator(".media-selection:checked")).to_have_count(2)
    select_all.press("Tab")
    clear = group.get_by_role("button", name="取消全選", exact=True)
    expect(clear).to_be_focused()
    clear.press("Space")
    expect(page.locator(".media-selection:checked")).to_have_count(0)
    clear.press("Tab")
    images = group.get_by_role("button", name="只選圖片")
    expect(images).to_be_focused()
    images.press("Enter")
    expect(page.locator(".media-selection:checked")).to_have_count(2)
    images.press("Tab")
    videos = group.get_by_role("button", name="只選影片")
    expect(videos).to_be_focused()
    videos.press("Enter")
    expect(page.locator(".media-selection:checked")).to_have_count(0)
    expect(group.locator(".selection-summary")).to_have_text("0 / 2 已選取")
    expect(group.get_by_role("button", name="下載選取項目")).to_be_disabled()
    assert group.locator("button[aria-pressed]").count() == 0


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_selection_groups_stay_independent(
    engine_page: Page, base_url: str, download_request_methods: list[str]
) -> None:
    """批次選取動作只更新所屬群組，沒有額外分析、下載或檢視。"""
    page = engine_page
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 2)
    groups = page.get_by_role("group", name="媒體選取操作", exact=True)
    expect(groups).to_have_count(2)
    groups.nth(0).get_by_role("button", name="全選", exact=True).press("Enter")
    expect(groups.nth(0).locator(".selection-summary")).to_have_text("1 / 1 已選取")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("0 / 1 已選取")
    page.locator("#next-result-group").click()
    groups.nth(1).get_by_role("button", name="全選", exact=True).press("Space")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("1 / 1 已選取")
    groups.nth(1).get_by_role("button", name="取消全選", exact=True).press("Enter")
    expect(groups.nth(0).locator(".selection-summary")).to_have_text("1 / 1 已選取")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("0 / 1 已選取")
    assert len(calls) == 2
    assert download_request_methods == []
    expect(page.locator("#media-viewer")).to_be_hidden()
