"""驗證複製檔名入口的可區分名稱、鍵盤啟動及重建順序。"""

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
def test_named_copy_writes_only_the_chosen_card_filename(
    engine_page: Page, base_url: str, width: int, key: str
) -> None:
    """跨組同檔名仍有不同名稱，指定第二項只寫該檔名且維持回饋與焦點。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(
        """/** 只替換外部剪貼簿寫入邊界，不模擬產品按鈕或回饋。 */ {
          window.copiedNames = [];
          Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
            /** 記錄產品實際送出的檔名。 */
            writeText: async text => { window.copiedNames.push(text); }
          }});
        }"""
    )
    calls: list[str] = []
    download_requests: list[str] = []

    def extraction(route: Route) -> None:
        """每組第一項同名、第二項各自不同，讓錯誤目標無法通過文字斷言。"""
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
                        "filename": "shared.png" if index == 1 else f"group-{number}-item-2.png",
                    }
                    for index in [1, 2]
                ],
            }
        )

    def record_download(request: Request) -> None:
        """直接觀察下載請求，不以只記錄view的fixture推論零attachment。"""
        if request.url.endswith("/download"):
            download_requests.append(request.url)

    page.route("**/api/extractions", extraction)
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
                "button", name=f"複製檔名，項目 {index:02d}，第 {number} 組", exact=True
            )
            expect(action).to_have_text("複製檔名")
            expect(action).to_have_attribute("type", "button")
    action = page.get_by_role("button", name="複製檔名，項目 02，第 2 組", exact=True)
    action.focus()
    action.press(key)
    feedback = page.locator(".result-group").nth(1).locator(".copy-feedback").nth(1)
    expect(feedback).to_have_text("已複製檔名")
    expect(feedback).to_be_visible()
    expect(action).to_have_accessible_name("複製檔名，項目 02，第 2 組")
    expect(action).to_be_enabled()
    expect(action).to_be_focused()
    assert page.evaluate("/** 讀取產品送出的文字。 */ () => window.copiedNames") == [
        "group-2-item-2.png"
    ]
    expect(page.locator(".copy-feedback").filter(has_text="已複製檔名")).to_have_count(1)
    expect(page.locator("dialog[open]")).to_have_count(0)
    expect(page.locator(".media-selection:checked")).to_have_count(0)
    assert download_requests == []
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_copy_names_keep_group_number_after_recovery_and_reset_for_single(
    engine_page: Page, base_url: str
) -> None:
    """第二組重新分析後仍以第二組命名，單筆取代後回第一組。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)

    def expired(route: Route) -> None:
        """只以合成過期回應啟用現有復原流程，不改renderer。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    page.get_by_role("button", name="下載項目 01，第 2 組", exact=True).click()
    second = page.locator(".result-group").nth(1)
    second.locator(".group-recovery .re-analyze").click()
    expect(second.locator(".group-recovery .re-analyze")).to_have_count(0)
    expect(
        second.get_by_role("button", name="複製檔名，項目 01，第 2 組", exact=True)
    ).to_be_enabled()
    expect(
        page.get_by_role("button", name="複製檔名，項目 01，第 1 組", exact=True)
    ).to_be_enabled()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(
        page.get_by_role("button", name="複製檔名，項目 01，第 1 組", exact=True)
    ).to_be_enabled()
    expect(page.get_by_role("button", name="複製檔名，項目 01，第 2 組", exact=True)).to_have_count(
        0
    )
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
