"""驗證重新分析成功後的鍵盤定位及不搶已移走的焦點。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("entry", ["card", "viewer"])
@pytest.mark.parametrize("move_away", [False, True])
def test_recovered_group_restores_only_its_own_keyboard_context(
    engine_page: Page, base_url: str, entry: str, move_away: bool
) -> None:
    """同群組重建後定位新標題；使用者等待時移到外部欄位則保持不動。"""
    page = engine_page
    viewer_workflow.open_results(page, base_url)
    pending: list[Route] = []

    def recover(route: Route) -> None:
        """延後合成回應以便執行真實焦點操作，不替換產品focus方法。"""
        pending.append(route)
        page.evaluate(
            "/** 標示邊界請求已到達，避免request事件早於route handler完成。 */ "
            "() => { window.recoveryWaiting = true; }"
        )

    def expired(route: Route) -> None:
        """只用固定410觸發既有復原入口，不提供真實平台URL。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/extractions", recover)
    page.route("**/api/media/*/download", expired)
    page.route("**/api/media/*/view", expired)
    group = page.locator(".result-group")
    if entry == "card":
        group.locator(".download-action").first.click()
        trigger = group.locator(".group-recovery .re-analyze")
    else:
        group.locator(".media-view").first.click()
        trigger = page.locator("#viewer-recovery")
    expect(trigger).to_be_visible()
    trigger.focus()
    trigger.press("Enter")
    page.wait_for_function(
        "/** 等待測試邊界實際收到請求，而不是等待任意毫秒。 */ "
        "() => window.recoveryWaiting === true"
    )
    expect(page.locator("#media-viewer")).to_be_hidden()
    if move_away:
        page.locator("#post-url").focus()
    assert len(pending) == 1
    pending[0].fulfill(json=workflow.SUCCESS_PAYLOAD)
    expect(group.locator(".media-card")).to_have_count(1)
    heading = group.locator(".results-heading h2")
    if move_away:
        expect(page.locator("#post-url")).to_be_focused()
    else:
        expect(heading).to_be_focused()
        expect(heading).to_have_attribute("tabindex", "-1")
        heading.press("Tab")
        expect(group.locator(".source-link")).to_be_focused()
    expect(group.locator(".selection-summary")).to_have_text("0 / 1 已選取")
    expect(group.locator(".group-recovery .re-analyze")).to_have_count(0)
    assert len(pending) == 1
