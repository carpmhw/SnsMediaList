"""驗證媒體選取操作群組名稱及原生篩選的群組隔離。"""

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
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_named_selection_groups_keep_native_actions_and_isolation(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """可由群組名稱操作圖片／影片篩選，原生Tab與另一組選取皆不變。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    calls: list[str] = []
    media_requests: list[str] = []

    def extraction(route: Route) -> None:
        """兩組各含一張圖片及一段影片，保留產品真正類型篩選。"""
        source = route.request.post_data_json["url"]
        calls.append(source)
        route.fulfill(
            json={
                **workflow.SUCCESS_PAYLOAD,
                "post_url": source,
                "media": [
                    workflow.SUCCESS_PAYLOAD["media"][0],
                    workflow.X_VIDEO_PAYLOAD["media"][0],
                ],
            }
        )

    def record_media(request: Request) -> None:
        """獨立記錄原始媒體請求，選取動作不得啟動下載或檢視。"""
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
    expect(page.locator(".result-group")).to_have_count(2)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    first = page.get_by_role("group", name="媒體選取操作，第 1 組", exact=True)
    second = page.get_by_role("group", name="媒體選取操作，第 2 組", exact=True)
    expect(first).to_have_count(1)
    expect(second).to_have_count(1)
    assert second.get_attribute("tabindex") is None
    first.get_by_role("button", name="全選", exact=True).press(key)
    page.locator("#next-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")
    select_all = second.get_by_role("button", name="全選", exact=True)
    select_all.focus()
    select_all.press(key)
    expect(second.locator(".selection-summary")).to_have_text("2 / 2 已選取")
    select_all.press("Tab")
    clear = second.get_by_role("button", name="取消全選", exact=True)
    expect(clear).to_be_focused()
    clear.press(key)
    expect(second.locator(".selection-summary")).to_have_text("0 / 2 已選取")
    clear.press("Tab")
    images = second.get_by_role("button", name="只選圖片", exact=True)
    expect(images).to_be_focused()
    images.press(key)
    expect(page.get_by_role("checkbox", name="選取項目 01 第 2 組", exact=True)).to_be_checked()
    expect(page.get_by_role("checkbox", name="選取項目 02 第 2 組", exact=True)).not_to_be_checked()
    images.press("Tab")
    videos = second.get_by_role("button", name="只選影片", exact=True)
    expect(videos).to_be_focused()
    videos.press(key)
    expect(page.get_by_role("checkbox", name="選取項目 01 第 2 組", exact=True)).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="選取項目 02 第 2 組", exact=True)).to_be_checked()
    expect(first.locator(".selection-summary")).to_have_text("2 / 2 已選取")
    expect(second.locator(".selection-summary")).to_have_text("1 / 2 已選取")
    expect(second.get_by_role("button", name="下載選取項目", exact=True)).to_be_enabled()
    expect(second.locator("button[aria-pressed]")).to_have_count(0)
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert media_requests == []
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_selection_group_name_survives_recovery_and_resets_for_single(
    engine_page: Page, base_url: str
) -> None:
    """原地復原仍是第二組操作區，單筆取代後不留舊組號或選取。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)

    def expired(route: Route) -> None:
        """僅提供合成過期回應，使用既有群組復原流程。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    page.get_by_role("button", name="重新分析此項，第 2 組", exact=True).click()
    expect(page.get_by_role("button", name="重新分析此項，第 2 組", exact=True)).to_have_count(0)
    second = page.get_by_role("group", name="媒體選取操作，第 2 組", exact=True)
    expect(second.locator(".selection-summary")).to_have_text("0 / 1 已選取")
    expect(second.get_by_role("button", name="下載選取項目", exact=True)).to_be_disabled()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    first = page.get_by_role("group", name="媒體選取操作，第 1 組", exact=True)
    expect(first).to_have_count(1)
    expect(second).to_have_count(0)
    expect(first.locator(".selection-summary")).to_have_text("0 / 1 已選取")
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
