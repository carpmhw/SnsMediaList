"""驗證個別下載的可區分名稱、原生鍵盤啟動及群組重建。"""

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
def test_named_download_activates_only_its_group_and_item(
    engine_page: Page, base_url: str, width: int, key: str, download_request_methods: list[str]
) -> None:
    """即使同檔名，仍可用明確名稱下載第二組第二項，不動選取或開viewer。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    extraction_calls: list[str] = []
    preflight_urls: list[str] = []
    body = workflow.X_VIDEO_BYTES

    def extraction(route: Route) -> None:
        """每組兩項使用不同token但相同檔名，保留真實前端批次與下載流程。"""
        source = route.request.post_data_json["url"]
        extraction_calls.append(source)
        number = source.rsplit("/", 1)[1]
        route.fulfill(
            json={
                **workflow.SUCCESS_PAYLOAD,
                "post_url": source,
                "media": [
                    {
                        **workflow.X_VIDEO_PAYLOAD["media"][0],
                        "download_url": (
                            workflow.X_VIDEO_PAYLOAD["media"][0]["download_url"]
                            if number == "2" and index == 2
                            else f"/api/media/g{number}-i{index}/download"
                        ),
                    }
                    for index in [1, 2]
                ],
            }
        )

    def record_preflight(request: Request) -> None:
        """觀察前端預檢；原生GET由下載URL、內容與本機server紀錄確認。"""
        if request.method == "HEAD" and request.url.endswith("/download"):
            preflight_urls.append(request.url)

    page.route("**/api/extractions", extraction)
    page.on("request", record_preflight)
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
                "button", name=f"下載項目 {index:02d}，第 {number} 組", exact=True
            )
            expect(action).to_have_text("下載")
            expect(action).to_have_attribute("type", "button")
    action = page.get_by_role("button", name="下載項目 02，第 2 組", exact=True)
    action.focus()
    expect(action).to_be_focused()
    with page.expect_download() as started:
        action.press(key)
    attachment = started.value
    assert attachment.failure() is None
    path = attachment.path()
    assert path is not None and path.read_bytes() == body
    assert attachment.suggested_filename == workflow.X_VIDEO_FILENAME
    expect(action).to_be_enabled()
    expect(action).to_have_accessible_name("下載項目 02，第 2 組")
    expect(page.locator("dialog[open]")).to_have_count(0)
    expect(page.locator(".media-selection:checked")).to_have_count(0)
    assert preflight_urls == [f"{base_url}/api/media/opaque-x-video-download/download"]
    assert attachment.url == f"{base_url}/api/media/opaque-x-video-download/download"
    assert download_request_methods == ["HEAD", "GET"]
    assert extraction_calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
    ]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_download_names_keep_group_number_after_recovery_and_reset_for_single(
    engine_page: Page, base_url: str
) -> None:
    """重新分析第二組仍是第二組，單筆取代後下載入口重設為第一組。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)

    def expired(route: Route) -> None:
        """用固定410引出原本的重新分析入口，不連線外部平台。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    second = page.locator(".result-group").nth(1)
    second.locator(".group-recovery .re-analyze").click()
    expect(second.locator(".group-recovery .re-analyze")).to_have_count(0)
    expect(second.get_by_role("button", name="下載項目 01，第 2 組", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="下載項目 01，第 1 組", exact=True)).to_be_enabled()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.get_by_role("button", name="下載項目 01，第 1 組", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="下載項目 01，第 2 組", exact=True)).to_have_count(0)
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
