"""驗證書籤安裝區尺寸與原生展開、鍵盤及唯讀選取。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
def test_bookmarklet_installation_controls_are_readable_and_large_enough(
    engine_page: Page, base_url: str, width: int
) -> None:
    """summary與拖曳連結至少48px高，唯讀碼字級不低於root字級且可換行。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    summary = page.locator("#bookmarklet-tools summary")
    expect(summary).to_have_css("min-block-size", "48px")
    bounds = summary.bounding_box()
    assert bounds
    summary_height = bounds["height"]
    summary.click()
    link = page.locator("#bookmarklet-link")
    bounds = link.bounding_box()
    assert bounds
    link_height = bounds["height"]
    code = page.locator("#bookmarklet-code")
    fonts = code.evaluate(
        """/** 直接比較computed字級，不推測真機縮放結果。 */ el => [
          parseFloat(getComputedStyle(el).fontSize),
          parseFloat(getComputedStyle(document.documentElement).fontSize)]"""
    )
    assert summary_height >= 48 and link_height >= 48 and fonts[0] >= fonts[1], (
        summary_height,
        link_height,
        fonts,
    )
    expect(code).to_have_attribute("readonly", "")
    assert page.evaluate(
        "/** 長安裝碼只能在元件內換行或捲動，不能撐寬整頁。 */ "
        "() => document.documentElement.scrollWidth <= innerWidth"
    )


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_bookmarklet_disclosure_keeps_native_keyboard_and_readonly_selection(
    engine_page: Page, base_url: str
) -> None:
    """原生Enter／Space與Tab控制展開，Ctrl+A可完整選取且不執行安裝碼。"""
    page = engine_page
    extractions: list[str] = []
    page.on(
        "request",
        lambda request: extractions.append(request.url)
        if "/api/extractions" in request.url
        else None,
    )
    page.goto(base_url)
    tools = page.locator("#bookmarklet-tools")
    summary = tools.locator("summary")
    link = page.locator("#bookmarklet-link")
    code = page.locator("#bookmarklet-code")
    expect(code).to_be_hidden()
    summary.focus()
    summary.press("Tab")
    expect(link).not_to_be_focused()
    expect(code).not_to_be_focused()
    summary.focus()
    summary.press("Enter")
    expect(code).to_be_visible()
    expect(summary).to_be_focused()
    summary.press("Tab")
    expect(link).to_be_focused()
    link.press("Tab")
    expect(code).to_be_focused()
    original = code.input_value()
    assert original.startswith("javascript:")
    code.press("Control+a")
    assert code.evaluate(
        "/** 比對原生textarea的完整選取範圍，不讀系統剪貼簿。 */ "
        "el => el.selectionStart === 0 && el.selectionEnd === el.value.length"
    )
    code.press("x")
    expect(code).to_have_value(original)
    code.press("Shift+Tab")
    expect(link).to_be_focused()
    link.press("Shift+Tab")
    expect(summary).to_be_focused()
    summary.press("Space")
    expect(code).to_be_hidden()
    expect(summary).to_be_focused()
    assert tools.evaluate("/** 開關狀態完全由原生details提供。 */ el => el.open") is False
    assert extractions == []
    assert len(page.context.pages) == 1
