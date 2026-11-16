"""驗證群組重新分析按鈕的名稱、正確來源及原有復原語意。"""

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
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_named_recovery_reanalyzes_only_its_group(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """兩組過期可區分，第二組復原只替換自己的內容、選取與焦點。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 2)
    pending: list[Route] = []

    def expired(route: Route) -> None:
        """以固定過期預檢引出兩個真實群組復原入口。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    def recover(route: Route) -> None:
        """保留未完成API邊界，讓測試觀察原生disabled與固定名稱。"""
        pending.append(route)
        page.evaluate(
            "/** 標示請求已到達handler，避免request事件早於紀錄完成。 */ "
            "() => { window.recoveryWaiting = true; }"
        )

    page.route("**/api/media/*/download", expired)
    groups = page.locator(".result-group")
    groups.nth(0).locator(".media-selection").check()
    page.get_by_role("button", name="下載項目 01，第 1 組", exact=True).click()
    page.locator("#next-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")
    groups.nth(1).locator(".media-selection").check()
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    first = page.get_by_role("button", name="重新分析此項，第 1 組", exact=True)
    second = page.get_by_role("button", name="重新分析此項，第 2 組", exact=True)
    expect(first).to_have_text("重新分析此項")
    expect(second).to_have_text("重新分析此項")
    expect(second).to_have_attribute("type", "button")
    page.route("**/api/extractions", recover)
    second.focus()
    second.press(key)
    expect(second).to_be_disabled()
    expect(second).to_have_accessible_name("重新分析此項，第 2 組")
    page.wait_for_function(
        "/** 等待測試邊界而非固定毫秒。 */ () => window.recoveryWaiting === true"
    )
    assert len(pending) == 1
    assert pending[0].request.post_data_json == {"url": "https://x.com/creator/status/2"}
    pending[0].fulfill(
        json={
            **workflow.SUCCESS_PAYLOAD,
            "post_url": "https://x.com/creator/status/2",
            "description": "Renewed second result",
        }
    )
    expect(second).to_have_count(0)
    expect(groups.nth(1).locator(".post-description")).to_have_text("Renewed second result")
    expect(groups.nth(0).locator(".post-description")).to_have_text("A test post")
    expect(first).to_be_enabled()
    expect(groups.nth(0).locator(".selection-summary")).to_have_text("1 / 1 已選取")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("0 / 1 已選取")
    expect(
        groups.nth(1).get_by_role("heading", name="找到的媒體 第 2 組", exact=True)
    ).to_be_focused()
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]
    assert len(pending) == 1


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_failed_recovery_keeps_name_and_single_replacement_resets_number(
    engine_page: Page, base_url: str
) -> None:
    """失敗後仍可依原名稱重試；單筆取代後的復原入口不保留第二組編號。"""
    page = engine_page
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 2)
    recovery_calls: list[str] = []

    def expired(route: Route) -> None:
        """使用合成過期錯誤，不依賴真平台或Cookie。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    def failed(route: Route) -> None:
        """只讓一次復原失敗，保留產品原本的錯誤映射及控制釋放。"""
        recovery_calls.append(route.request.post_data_json["url"])
        route.fulfill(status=504, json={"code": "extraction_timeout"})

    page.route("**/api/media/*/download", expired)
    page.locator("#next-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    recovery = page.get_by_role("button", name="重新分析此項，第 2 組", exact=True)
    expect(recovery).to_be_visible()
    page.route("**/api/extractions", failed)
    recovery.press("Enter")
    group = page.locator(".result-group").nth(1)
    expect(group.locator(".group-status")).to_have_text("重新分析失敗。")
    expect(recovery).to_be_enabled()
    expect(recovery).to_have_accessible_name("重新分析此項，第 2 組")
    expect(recovery).to_have_text("重新分析此項")
    assert recovery_calls == ["https://x.com/creator/status/2"]
    page.unroute("**/api/extractions", failed)
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    page.get_by_role("button", name="下載項目 01，第 1 組", exact=True).click()
    expect(page.get_by_role("button", name="重新分析此項，第 1 組", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="重新分析此項，第 2 組", exact=True)).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
