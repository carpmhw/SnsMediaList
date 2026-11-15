"""驗證結果群組導覽的命名、受控軌道及原生鍵盤操作。"""

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
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_named_result_navigation_controls_track_and_preserves_keyboard(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """命名group保留原生按鈕順序及不循環邊界，單筆取代後不留下操作群組。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 3)
    navigation = page.get_by_role("group", name="結果群組導覽", exact=True)
    expect(navigation).to_be_visible()
    previous = navigation.get_by_role("button", name="上一組結果", exact=True)
    next_button = navigation.get_by_role("button", name="下一組結果", exact=True)
    position = navigation.get_by_role("status", name="結果群組位置", exact=True)
    expect(page.locator("#result-groups")).to_have_count(1)
    expect(previous).to_have_attribute("aria-controls", "result-groups")
    expect(next_button).to_have_attribute("aria-controls", "result-groups")
    expect(previous).to_have_attribute("type", "button")
    expect(next_button).to_have_attribute("type", "button")
    expect(previous).to_be_disabled()
    expect(position).to_have_text("第 1/3 組")
    next_button.focus()
    next_button.press(key)
    expect(position).to_have_text("第 2/3 組")
    expect(previous).to_be_enabled()
    expect(next_button).to_be_enabled()
    next_button.press("Shift+Tab")
    expect(previous).to_be_focused()
    previous.press("Tab")
    expect(next_button).to_be_focused()
    next_button.press(key)
    expect(position).to_have_text("第 3/3 組")
    expect(next_button).to_be_disabled()
    previous.focus()
    previous.press(key)
    expect(position).to_have_text("第 2/3 組")
    previous.press(key)
    expect(position).to_have_text("第 1/3 組")
    expect(previous).to_be_disabled()
    expect(page.locator(".result-group")).to_have_count(3)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/4")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.locator("#result-group-navigation")).to_be_hidden()
    expect(navigation).to_have_count(0)
    expect(page.get_by_role("button", name="上一組結果", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="下一組結果", exact=True)).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
        "https://x.com/creator/status/4",
    ]
