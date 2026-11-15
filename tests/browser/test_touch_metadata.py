"""以真實輸入能力媒體查詢驗證卡片資訊的無 hover 可見性。"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.fixture
def touch_engine_page(engine_page: Page) -> Iterator[Page]:
    """沿用匹配的引擎建立真正觸控context，結束時只清理本測試資源。"""
    browser = engine_page.context.browser
    assert browser is not None
    context = browser.new_context(has_touch=True)
    try:
        yield context.new_page()
    finally:
        context.close()


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 768, 1280])
def test_non_hover_metadata_is_visible_before_any_card_interaction(
    touch_engine_page: Page, base_url: str, width: int
) -> None:
    """寬窄觸控頁面都不需先選取卡片才能查看metadata，tap仍只執行選取。"""
    page = touch_engine_page
    page.set_viewport_size({"width": width, "height": 900})
    viewer_workflow.open_results(page, base_url)
    assert page.evaluate(
        "/** 核對真正瀏覽器媒體查詢，而非替換matchMedia。 */ () => "
        "matchMedia('(hover: none)').matches && matchMedia('(pointer: coarse)').matches"
    )
    card = page.locator(".media-card").first
    assert card.evaluate(
        "/** 在尚未hover或focus卡片時驗證預設資訊可見。 */ el => "
        "!el.matches(':hover') && !el.matches(':focus-within')"
    )
    metadata = card.locator(".media-metadata")
    expect(metadata).to_have_css("position", "absolute")
    expect(metadata).to_have_css("opacity", "1")
    expect(metadata).to_contain_text("1200 × 800")
    expect(metadata).to_contain_text("image-1.png")
    checkbox = card.get_by_role("checkbox", name="選取項目 01", exact=True)
    checkbox.tap()
    expect(checkbox).to_be_checked()
    expect(page.locator(".selection-summary")).to_have_text("1 / 2 已選取")
    expect(page.locator("#media-viewer")).to_be_hidden()
    assert page.evaluate(
        "/** 常駐資訊不能撐寬頁面。 */ () => document.documentElement.scrollWidth <= innerWidth"
    )


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_hover_and_keyboard_metadata_reveal_stays_unchanged(
    engine_page: Page, base_url: str
) -> None:
    """桌面保持hover與focus揭露，離開卡片後仍恢復原有隱藏狀態。"""
    page = engine_page
    page.set_viewport_size({"width": 1280, "height": 900})
    viewer_workflow.open_results(page, base_url)
    assert page.evaluate(
        "/** 確認此案例使用有hover能力的普通context。 */ () => matchMedia('(hover: hover)').matches"
    )
    card = page.locator(".media-card").first
    metadata = card.locator(".media-metadata")
    expect(metadata).to_have_css("opacity", "0")
    card.hover()
    expect(metadata).to_have_css("opacity", "1")
    page.mouse.move(0, 0)
    expect(metadata).to_have_css("opacity", "0")
    card.locator(".media-selection").focus()
    expect(metadata).to_have_css("opacity", "1")
    page.locator("#post-url").focus()
    expect(metadata).to_have_css("opacity", "0")
