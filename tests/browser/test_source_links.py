"""驗證原始來源連結的群組命名、新分頁提示及原生導覽。"""

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
def test_source_links_distinguish_groups_and_open_native_new_page(
    engine_page: Page, base_url: str, width: int
) -> None:
    """連結清單可分辨群組，Enter 開原來源且維持 opener／referrer 隔離。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    calls = open_groups(page, base_url, 2)
    for index, source in enumerate(calls, 1):
        link = page.get_by_role("link", name=f"開啟原始內容（新分頁），第 {index} 組", exact=True)
        expect(link).to_have_text("開啟原始內容（新分頁）")
        expect(link).to_have_attribute("href", source)
        expect(link).to_have_attribute("target", "_blank")
        assert set((link.get_attribute("rel") or "").split()) == {"noreferrer", "noopener"}

    def source_page(route: Route) -> None:
        """僅攔截合成來源頁，不讓測試連線真實平台。"""
        route.fulfill(content_type="text/html", body="<title>合成原始來源</title>")

    page.context.route("https://x.com/creator/status/*", source_page)
    link = page.get_by_role("link", name="開啟原始內容（新分頁），第 2 組", exact=True)
    link.focus()
    expect(link).to_be_focused()
    with page.context.expect_page() as opened:
        link.press("Enter")
    destination = opened.value
    destination.wait_for_load_state()
    assert destination.url == "https://x.com/creator/status/2"
    assert destination.evaluate("/** 檢查原生新分頁沒有 opener。 */ () => window.opener === null")
    assert (
        destination.evaluate("/** 檢查原始來源不收到 referrer。 */ () => document.referrer") == ""
    )
    destination.close()
    assert page.evaluate(
        "/** 新提示文字不應造成頁面水平溢出。 */ "
        "() => document.documentElement.scrollWidth <= innerWidth"
    )
    page.locator("#previous-result-group").click()
    expect(page.locator("#result-group-position")).to_have_text("第 1/2 組")
    for index in (1, 2):
        if index == 2:
            page.locator("#next-result-group").click()
            expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")
        link = page.get_by_role("link", name=f"開啟原始內容（新分頁），第 {index} 組", exact=True)
        assert link.evaluate(
            """/** 逐組量測連結四邊，避免整頁未溢出卻被軌道裁切。 */ el => {
              const track = document.querySelector('#result-groups');
              const outer = track.getBoundingClientRect();
              const box = el.getBoundingClientRect();
              const left = outer.left + track.clientLeft;
              const top = outer.top + track.clientTop;
              return box.width > 0 && box.height > 0
                && box.left >= left - 1 && box.right <= left + track.clientWidth + 1
                && box.top >= top - 1 && box.bottom <= top + track.clientHeight + 1;
            }"""
        )


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_source_link_group_number_survives_recovery_and_resets_for_single(
    engine_page: Page, base_url: str
) -> None:
    """第二組重新分析不改序號，批次被單筆取代後不沿用上一輪編號。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)

    def expired(route: Route) -> None:
        """用合成過期回應啟用既有群組復原按鈕。"""
        route.fulfill(status=410, headers={"x-sns-error-code": "token_expired"})

    page.route("**/api/media/*/download", expired)
    second = page.locator(".result-group").nth(1)
    second.locator(".download-action").first.click()
    second.locator(".group-recovery .re-analyze").click()
    expect(second.locator(".group-recovery .re-analyze")).to_have_count(0)
    expect(
        second.get_by_role("link", name="開啟原始內容（新分頁），第 2 組", exact=True)
    ).to_have_attribute("href", "https://x.com/creator/status/2")
    expect(
        page.locator(".result-group").first.get_by_role(
            "link", name="開啟原始內容（新分頁），第 1 組", exact=True
        )
    ).to_have_attribute("href", "https://x.com/creator/status/1")
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(
        page.get_by_role("link", name="開啟原始內容（新分頁），第 1 組", exact=True)
    ).to_have_attribute("href", "https://x.com/creator/status/3")
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_first_successful_source_after_failure_starts_at_group_one(
    engine_page: Page, base_url: str
) -> None:
    """失敗queue項目不佔結果組號，後續成功來源的連結從第一組開始。"""
    page = engine_page
    calls: list[str] = []

    def respond(route: Route) -> None:
        """第一來源合成失敗，第二來源提供完整成功payload。"""
        source = route.request.post_data_json["url"]
        calls.append(source)
        if source.endswith("/1"):
            route.fulfill(status=502, json={"code": "extraction_failed"})
        else:
            route.fulfill(json={**workflow.SUCCESS_PAYLOAD, "post_url": source})

    page.route("**/api/extractions", respond)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2"
    )
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    expect(page.locator(".queue-item-state").nth(0)).to_have_text("失敗")
    expect(page.locator(".queue-item-state").nth(1)).to_have_text("成功")
    expect(
        page.get_by_role("link", name="開啟原始內容（新分頁），第 1 組", exact=True)
    ).to_have_attribute("href", "https://x.com/creator/status/2")
    expect(
        page.get_by_role("link", name="開啟原始內容（新分頁），第 2 組", exact=True)
    ).to_have_count(0)
    assert calls == ["https://x.com/creator/status/1", "https://x.com/creator/status/2"]
