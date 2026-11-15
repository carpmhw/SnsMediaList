"""驗證原始媒體檢視器操作群組與既有原生鍵盤行為。"""

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
def test_viewer_named_actions_preserve_native_keyboard(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """命名群組保留逐項Tab、原生啟動與checkbox同步，不引入toolbar策略。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    calls = viewer_workflow.open_results(page, base_url)
    opener = page.locator(".media-view").first
    opener.focus()
    opener.press("Enter")
    dialog = page.get_by_role("dialog", name="原始媒體檢視", exact=True)
    actions = dialog.get_by_role("group", name="媒體檢視操作", exact=True)
    expect(actions).to_be_visible()
    expect(page.locator("#viewer-content img")).to_have_js_property("naturalWidth", 1600)
    previous = actions.get_by_role("button", name="上一則", exact=True)
    next_button = actions.get_by_role("button", name="下一則", exact=True)
    selected = actions.get_by_role("checkbox", name="選取此項", exact=True)
    size = actions.get_by_role("button", name="原始大小", exact=True)
    expect(previous).to_be_disabled()
    close = dialog.get_by_role("button", name="關閉", exact=True)
    expect(close).to_be_focused()
    close.press("Tab")
    expect(next_button).to_be_focused()
    next_button.press("Tab")
    expect(selected).to_be_focused()
    selected.press("Space")
    expect(selected).to_be_checked()
    selected.press("Shift+Tab")
    expect(next_button).to_be_focused()
    next_button.press(key)
    expect(dialog.locator("#viewer-position")).to_have_text("2 / 2")
    expect(next_button).to_be_disabled()
    expect(previous).to_be_enabled()
    expect(selected).not_to_be_checked()
    expect(page.locator("#viewer-content img")).to_have_js_property("naturalWidth", 1600)
    selected.focus()
    selected.press("Tab")
    expect(size).to_be_focused()
    size.press(key)
    expect(actions.get_by_role("button", name="適合視窗", exact=True)).to_be_focused()
    expect(dialog.locator("#viewer-content")).to_have_class("viewer-content actual-size")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    expect(page.locator(".media-selection").first).to_be_checked()
    expect(page.locator(".media-selection").nth(1)).not_to_be_checked()
    expect(page.locator(".selection-summary")).to_have_text("1 / 2 已選取")
    assert all(url.endswith("/view") for _, url in calls)
