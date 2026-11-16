"""驗證卡片檢視名稱的群組區分、正確原圖及焦點還原。"""

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_result_group_navigation import open_groups
from tests.media_fixtures import original_png

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_named_view_opens_only_the_chosen_group_and_item(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """依名稱開啟第二組第二項，原圖與位置正確且關閉回原入口。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    calls: list[str] = []
    views: list[tuple[str, str]] = []
    downloads: list[str] = []

    def extraction(route: Route) -> None:
        """每個來源給予兩個獨立token及檔名，避免錯目標通過檔名斷言。"""
        source = route.request.post_data_json["url"]
        calls.append(source)
        number = source.rsplit("/", 1)[1]
        route.fulfill(
            json={
                **workflow.SUCCESS_PAYLOAD,
                "post_url": source,
                "media": [
                    {
                        **workflow.SUCCESS_PAYLOAD["media"][0],
                        "filename": f"group-{number}-item-{index}.png",
                        "download_url": f"/api/media/g{number}-i{index}/download",
                    }
                    for index in [1, 2]
                ],
            }
        )

    def view(route: Route) -> None:
        """只替換原始媒體邊界，提供真正可解碼的PNG，不模擬viewer。"""
        views.append((route.request.method, route.request.url))
        if route.request.method == "HEAD":
            route.fulfill(status=204)
        else:
            route.fulfill(content_type="image/png", body=original_png())

    def record_download(request: Request) -> None:
        """獨立觀察download請求，不由view紀錄推論沒有下載。"""
        if request.url.endswith("/download"):
            downloads.append(request.url)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/*/view", view)
    page.on("request", record_download)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2"
    )
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".result-group")).to_have_count(2)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    for number in [1, 2]:
        for index in [1, 2]:
            action = page.get_by_role(
                "button", name=f"檢視項目 {index:02d}，第 {number} 組", exact=True
            )
            expect(action).to_have_text("檢視")
            expect(action).to_have_attribute("type", "button")
    action = page.get_by_role("button", name="檢視項目 02，第 2 組", exact=True)
    action.focus()
    action.press(key)
    dialog = page.get_by_role("dialog", name="原始媒體檢視", exact=True)
    expect(dialog).to_be_visible()
    expect(page.locator("#viewer-filename")).to_have_text("group-2-item-2.png")
    expect(page.locator("#viewer-position")).to_have_text("2 / 2")
    expect(page.locator("#viewer-content img")).to_have_js_property("naturalWidth", 1600)
    expect(page.locator("#viewer-next")).to_be_disabled()
    expect(page.locator("#viewer-previous")).to_be_enabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(action).to_be_focused()
    expect(action).to_have_accessible_name("檢視項目 02，第 2 組")
    expect(page.locator(".media-selection:checked")).to_have_count(0)
    assert views == [
        ("HEAD", f"{base_url}/api/media/g2-i2/view"),
        ("GET", f"{base_url}/api/media/g2-i2/view"),
    ]
    assert downloads == []
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_view_names_keep_group_number_after_recovery_and_reset_for_single(
    engine_page: Page, base_url: str
) -> None:
    """第二組重新分析維持檢視命名，單筆取代後不保留前輪組號。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)

    def expired(route: Route) -> None:
        """用合成410觸發現有群組復原，不讀取平台或Cookie。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    second = page.locator(".result-group").nth(1)
    second.locator(".group-recovery .re-analyze").click()
    expect(second.locator(".group-recovery .re-analyze")).to_have_count(0)
    expect(second.get_by_role("button", name="檢視項目 01，第 2 組", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="檢視項目 01，第 1 組", exact=True)).to_be_enabled()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.get_by_role("button", name="檢視項目 01，第 1 組", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="檢視項目 01，第 2 組", exact=True)).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
