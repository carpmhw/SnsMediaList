"""驗證原生選取標籤的群組上下文、點擊區及選取隔離。"""

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_result_group_navigation import open_groups

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("activation", ["Space", "label"])
def test_named_selection_preserves_native_label_and_group_isolation(
    engine_page: Page, base_url: str, width: int, activation: str
) -> None:
    """四個原生標籤可區分，Space或標籤邊緣只變更第二組第二項。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    calls: list[str] = []
    media_requests: list[str] = []

    def extraction(route: Route) -> None:
        """每組回傳兩個項目，僅替換API邊界以使用真正選取流程。"""
        source = route.request.post_data_json["url"]
        calls.append(source)
        route.fulfill(
            json={
                **workflow.SUCCESS_PAYLOAD,
                "post_url": source,
                "media": [workflow.SUCCESS_PAYLOAD["media"][0]] * 2,
            }
        )

    def record_media(request: Request) -> None:
        """獨立觀察下載及原始檢視，避免由擷取紀錄推論無其他請求。"""
        if request.url.endswith(("/download", "/view")):
            media_requests.append(request.url)

    page.route("**/api/extractions", extraction)
    page.on("request", record_media)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2"
    )
    page.locator("#analyze-batch-button").click()
    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    for number in [1, 2]:
        for index in [1, 2]:
            name = f"選取項目 {index:02d} 第 {number} 組"
            checkbox = page.get_by_role("checkbox", name=name, exact=True)
            expect(checkbox).not_to_be_checked()
            expect(page.get_by_label(name, exact=True)).to_have_count(1)
            assert checkbox.get_attribute("aria-label") is None
            assert checkbox.get_attribute("aria-labelledby") is None
    checkbox = page.get_by_role("checkbox", name="選取項目 02 第 2 組", exact=True)
    label = groups.nth(1).locator(".media-selection-label").nth(1)
    expect(label).to_have_css("min-block-size", "44px")
    bounds = label.bounding_box()
    assert bounds and bounds["height"] >= 44 and bounds["width"] >= 44
    if activation == "Space":
        checkbox.focus()
        checkbox.press("Space")
    else:
        label.click(position={"x": bounds["width"] - 3, "y": 3})
    expect(checkbox).to_be_checked()
    expect(checkbox).to_be_focused()
    expect(page.locator(".media-selection:checked")).to_have_count(1)
    expect(groups.nth(0).locator(".selection-summary")).to_have_text("0 / 2 已選取")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("1 / 2 已選取")
    expect(groups.nth(0).get_by_role("button", name="下載選取項目")).to_be_disabled()
    expect(groups.nth(1).get_by_role("button", name="下載選取項目")).to_be_enabled()
    checkbox.press("Space")
    expect(checkbox).not_to_be_checked()
    expect(checkbox).to_be_focused()
    expect(checkbox).to_have_accessible_name("選取項目 02 第 2 組")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("0 / 2 已選取")
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert media_requests == []
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_selection_names_keep_group_number_after_recovery_and_reset_for_single(
    engine_page: Page, base_url: str
) -> None:
    """重新分析清空選取但保留第二組名稱，單筆取代後重設第一組。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)
    page.locator(".result-group").nth(1).locator(".media-selection").check()

    def expired(route: Route) -> None:
        """只用合成過期預檢引出現有復原入口，不改產品狀態。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    second = page.locator(".result-group").nth(1)
    second.locator(".group-recovery .re-analyze").click()
    expect(second.locator(".group-recovery .re-analyze")).to_have_count(0)
    expect(
        second.get_by_role("checkbox", name="選取項目 01 第 2 組", exact=True)
    ).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="選取項目 01 第 1 組", exact=True)).not_to_be_checked()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.get_by_role("checkbox", name="選取項目 01 第 1 組", exact=True)).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="選取項目 01 第 2 組", exact=True)).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
