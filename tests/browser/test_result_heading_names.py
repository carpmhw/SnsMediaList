"""驗證結果標題群組上下文與重新分析後的原生鍵盤定位。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_result_group_navigation import open_groups

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
def test_result_headings_distinguish_groups_without_new_tab_stops(
    engine_page: Page, base_url: str, width: int
) -> None:
    """三組h2可在標題清單區分，不改可見文字或新增一般Tab停靠。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    calls = open_groups(page, base_url, 3)
    groups = page.locator(".result-group")
    expect(groups.get_by_role("heading", level=2)).to_have_count(3)
    for index in [1, 2, 3]:
        heading = page.get_by_role("heading", name=f"找到的媒體 第 {index} 組", level=2, exact=True)
        expect(heading).to_have_count(1)
        expect(groups.nth(index - 1).get_by_role("heading", level=2)).to_have_accessible_name(
            f"找到的媒體 第 {index} 組"
        )
        assert heading.evaluate(
            "/** 標題仍由既有原生可見文字起頭，不改成ARIA覆寫。 */ "
            "el => el.tagName === 'H2' && el.firstChild.textContent === '找到的媒體'"
        )
        assert heading.get_attribute("aria-label") is None
        assert heading.get_attribute("aria-labelledby") is None
        assert heading.get_attribute("tabindex") is None
    expect(page.locator("#results-heading")).to_have_text("找到的媒體")
    assert page.evaluate(
        "/** 離屏標題的隱藏上下文不能擴大整頁捲動範圍。 */ "
        "() => document.documentElement.scrollWidth <= innerWidth"
    )
    page.emulate_media(reduced_motion="reduce")
    page.locator("#next-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 2/3 組")
    assert page.evaluate(
        "/** 切換後絕對定位文字也必須留在自己的群組內。 */ "
        "() => document.documentElement.scrollWidth <= innerWidth && window.scrollX === 0"
    )
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("entry", ["card", "viewer"])
def test_recovered_heading_keeps_group_context_focus_and_tab_successor(
    engine_page: Page, base_url: str, entry: str
) -> None:
    """第二組復原保留組號及標題焦點，單筆取代後標題回到第一組。"""
    page = engine_page
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 2)
    page.locator("#next-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")

    def expired(route: Route) -> None:
        """僅於授權預檢回應過期，保留真實card／viewer復原流程。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.route("**/api/media/*/view", expired)
    group = page.locator(".result-group").nth(1)
    if entry == "card":
        page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
        trigger = group.locator(".group-recovery .re-analyze")
    else:
        page.get_by_role("button", name="檢視項目 01，第 2 組", exact=True).click()
        trigger = page.locator("#viewer-recovery")
    expect(trigger).to_be_visible()
    trigger.focus()
    trigger.press("Enter")
    heading = group.get_by_role("heading", name="找到的媒體 第 2 組", level=2, exact=True)
    expect(heading).to_be_focused()
    expect(heading).to_have_attribute("tabindex", "-1")
    expect(page.locator("#media-viewer")).to_be_hidden()
    heading.press("Tab")
    expect(
        group.get_by_role("link", name="開啟原始內容（新分頁），第 2 組", exact=True)
    ).to_be_focused()
    expect(group.locator(".selection-summary")).to_have_text("0 / 1 已選取")
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(
        page.get_by_role("heading", name="找到的媒體 第 1 組", level=2, exact=True)
    ).to_have_count(1)
    expect(
        page.get_by_role("heading", name="找到的媒體 第 2 組", level=2, exact=True)
    ).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
