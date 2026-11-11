"""驗證頁首品牌與狀態細線的最小整理，不改既有工作流程。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_media_viewer import open_results

page = workflow.page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


def test_brand_is_a_separate_page_header(page: Page, base_url: str) -> None:
    """品牌保留一次並成為獨立頁首，不再是hero小標或新增Tab停靠點。"""
    page.goto(base_url)
    banner = page.get_by_role("banner")
    expect(banner).to_have_count(1)
    expect(banner).to_have_text("SNS / MEDIA")
    expect(page.get_by_text("SNS / MEDIA", exact=True)).to_have_count(1)
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    expect(page.get_by_role("heading", level=1)).to_have_text("把媒體來源轉成可控的下載清單。")
    assert banner.evaluate(
        """/** 檢查頁首在主內容之外，而非hero內的裝飾標籤。 */ el =>
          !el.closest('main') && Boolean(
            el.compareDocumentPosition(document.querySelector('.workspace-hero'))
              & Node.DOCUMENT_POSITION_FOLLOWING)"""
    )
    expect(
        banner.locator("a, button, input, textarea, select, [tabindex]:not([tabindex='-1'])")
    ).to_have_count(0)
    brand = banner.locator(".brand-mark")
    expect(brand).to_have_css("letter-spacing", "normal")
    assert brand.evaluate(
        """/** 頁首標誌使用正文系統字型，不沿用等寬的小標樣式。 */ el =>
          getComputedStyle(el).fontFamily === getComputedStyle(document.body).fontFamily"""
    )


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_page_header_aligns_without_overflow(page: Page, base_url: str, width: int) -> None:
    """各版寬頁首與主內容對齊、不遮擋主標，鍵盤與分析入口仍可用。"""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    banner = page.get_by_role("banner")
    expect(banner).to_have_count(1)
    header = banner.bounding_box()
    main = page.locator("main").bounding_box()
    hero = page.locator(".workspace-hero").bounding_box()
    assert header and main and hero
    assert abs(header["x"] - main["x"]) <= 1
    assert abs((header["x"] + header["width"]) - (main["x"] + main["width"])) <= 1
    assert header["y"] + header["height"] <= hero["y"]
    assert page.evaluate(
        """/** 檢查整頁沒有水平溢出。 */
        () => document.documentElement.scrollWidth <= innerWidth"""
    )
    page.locator("#single-mode-button").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#batch-mode-button")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#batch-mode-button")).to_have_attribute("aria-pressed", "true")
    page.locator("#single-mode-button").click()
    analyze = page.get_by_role("button", name="分析", exact=True)
    analyze.scroll_into_view_if_needed()
    expect(analyze).to_be_in_viewport()
    expect(analyze).to_be_enabled()


def test_error_details_keep_recovery_with_thin_rules(page: Page, base_url: str) -> None:
    """單項及選取下載錯誤仍有文字與恢復操作，只縮小側線。"""
    open_results(page, base_url)

    def expired_download(route: Route) -> None:
        """以合成過期授權拒絕HEAD，不啟動真正附件下載或連外。"""
        assert route.request.method == "HEAD"
        route.fulfill(
            status=410,
            headers={"X-SNS-Error-Code": "token_expired"},
            json={"code": "token_expired"},
        )

    page.route("**/api/media/*/download", expired_download)
    page.locator(".download-action").first.click()
    status = page.locator('.group-status[data-state="error"]')
    error = page.locator(".group-error")
    expect(status).to_have_text("無法啟動下載。")
    expect(error).to_contain_text("下載參照已過期")
    expect(error).to_be_visible()
    expect(status).to_have_css("border-left-width", "1px")
    expect(error).to_have_css("border-left-width", "1px")
    recovery = page.get_by_role("button", name="重新分析此項", exact=True)
    expect(recovery).to_be_enabled()
    recovery.click()
    expect(error).to_be_hidden()
    expect(error).to_have_text("")
    expect(page.locator(".group-status")).not_to_have_attribute("data-state", "error")
    page.get_by_role("button", name="全選", exact=True).click()
    page.get_by_role("button", name="下載選取項目", exact=True).click()
    summary = page.locator('.download-summary[data-state="error"]')
    expect(summary).to_contain_text("無法啟動")
    expect(summary).to_have_attribute("aria-live", "polite")
    expect(summary).to_have_css("border-left-width", "1px")
    expect(recovery).to_be_enabled()
    recovery.click()
    expect(page.locator(".download-summary")).to_have_text("")
    expect(error).to_be_hidden()


def test_batch_queue_keeps_textual_states_with_thin_rules(page: Page, base_url: str) -> None:
    """真實queue保留等待／執行中／成功／失敗文字，長URL不破壞窄版。"""
    page.set_viewport_size({"width": 375, "height": 900})
    urls = [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3?note=" + "a" * 600,
    ]
    held: list[Route] = []
    submitted: list[str] = []

    def extraction(route: Route) -> None:
        """暫留第一項以呈現等待狀態，再合成一失敗及一成功結果。"""
        source = route.request.post_data_json["url"]
        submitted.append(source)
        if source == urls[0]:
            held.append(route)
        elif source == urls[1]:
            route.fulfill(status=400, json={"code": "unsupported_url"})
        else:
            assert source == urls[2]
            route.fulfill(json={**workflow.SUCCESS_PAYLOAD, "post_url": source})

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析", exact=True).click()
    page.locator("#batch-post-urls").fill("\n".join(urls))
    with page.expect_request("**/api/extractions"):
        page.get_by_role("button", name="開始批次分析", exact=True).click()
    items = page.locator(".queue-item")
    expect(items).to_have_count(3)
    expect(items.nth(0).locator(".queue-item-state")).to_have_text("執行中")
    expect(items.nth(1).locator(".queue-item-state")).to_have_text("等待中")
    expect(items.nth(2).locator(".queue-item-state")).to_have_text("等待中")
    for item in items.all():
        expect(item).to_have_css("border-left-width", "1px")
    assert len(held) == 1
    held[0].fulfill(json=workflow.SUCCESS_PAYLOAD)
    expect(page.locator("#queue-progress")).to_contain_text("2 / 3 已完成")
    expect(items.nth(0).locator(".queue-item-state")).to_have_text("成功")
    expect(items.nth(1).locator(".queue-item-state")).to_have_text("失敗")
    expect(items.nth(2).locator(".queue-item-state")).to_have_text("成功")
    assert submitted == urls
    expect(items.nth(1).locator(".queue-item-error")).to_be_visible()
    expect(page.locator("#queue-progress")).to_have_attribute("aria-live", "polite")
    expect(page.get_by_role("button", name="開始批次分析", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="停止批次分析", exact=True)).to_be_hidden()
    assert page.evaluate(
        """/** 檢查完成queue與長URL仍無整頁水平溢出。 */
        () => document.documentElement.scrollWidth <= innerWidth"""
    )
