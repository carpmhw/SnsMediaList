"""以真實圖片錯誤驗證回退只替換媒體、不移除卡片控制。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("kind", ["image", "video"])
def test_failed_preview_preserves_selected_focused_control_and_advances_queue(
    engine_page: Page, base_url: str, kind: str
) -> None:
    """兩次HTTP失敗只移除img，已勾選控制、焦點與metadata節點均保持。"""
    page = engine_page
    payload = {
        **workflow.SUCCESS_PAYLOAD,
        "media": [
            {
                **workflow.SUCCESS_PAYLOAD["media"][0],
                "media_type": kind,
                "preview_url": f"/api/media/preview-{index}/preview",
            }
            for index in range(2)
        ],
    }
    pending: list[Route] = []
    attempts: list[str] = []

    def extraction(route: Route) -> None:
        """只提供合成結果，不連線來源平台。"""
        route.fulfill(json=payload)

    def preview(route: Route) -> None:
        """第一項由測試釋放真實失敗回應，第二項實際解碼圖片。"""
        attempts.append(route.request.url)
        if "/preview-0/" in route.request.url:
            if not pending:
                pending.append(route)
            else:
                route.fulfill(status=502, body="synthetic failure")
        else:
            route.fulfill(content_type="image/png", body=workflow.PREVIEW_BYTES)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/*/preview", preview)
    page.goto(base_url)
    page.locator("#post-url").fill("https://x.com/creator/status/1")
    with page.expect_request("**/api/media/preview-0/preview"):
        page.locator("#analyze-button").click()
    first = page.locator(".media-card").first
    checkbox = first.get_by_role("checkbox", name="選取項目 01", exact=True)
    checkbox.check()
    checkbox.focus()
    checkbox.evaluate(
        "/** 保存節點身分以捕捉重建或刪除，不替換任何焦點API。 */ "
        "el => { window.originalSelection = el; }"
    )
    with page.expect_request("**/api/media/preview-0/preview"):
        pending[0].fulfill(status=502, body="synthetic failure")
    expect(first.locator(".fallback-tile")).to_be_visible()
    expect(first.locator(".media-visual img")).to_have_count(0)
    expect(checkbox).to_be_checked()
    expect(checkbox).to_be_focused()
    assert checkbox.evaluate(
        "/** 回退不得重建原生控制或丟掉原節點狀態。 */ el => el === window.originalSelection"
    )
    expect(first.locator(".media-metadata")).to_have_count(1)
    expect(first.locator(".media-metadata")).to_contain_text("x-1-1.jpg")
    expect(page.locator(".selection-summary")).to_have_text("1 / 2 已選取")
    checkbox.press("Space")
    expect(page.locator(".selection-summary")).to_have_text("0 / 2 已選取")
    expect(first.locator(".download-action")).to_be_enabled()
    expect(first.locator(".copy-filename")).to_be_enabled()
    expect(first.locator(".media-view")).to_be_enabled()
    page.wait_for_function(
        "/** 驗證queue確實繼續下一張且可解碼，不mock load事件。 */ () => "
        "document.querySelectorAll('.media-card')[1].querySelector('img')?.naturalWidth > 0"
    )
    assert len([url for url in attempts if "/preview-0/" in url]) == 2
    assert len([url for url in attempts if "/preview-1/" in url]) == 1


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_late_error_after_loaded_preview_preserves_controls(
    engine_page: Page, base_url: str
) -> None:
    """已成功圖片後續真正HTTP失敗的非active路徑也不刪除控制。"""
    page = engine_page
    calls: list[str] = []

    def extraction(route: Route) -> None:
        """為成功後錯誤路徑提供單張合成預覽。"""
        media = {
            **workflow.SUCCESS_PAYLOAD["media"][0],
            "preview_url": "/api/media/late/preview",
        }
        route.fulfill(json={**workflow.SUCCESS_PAYLOAD, "media": [media]})

    def preview(route: Route) -> None:
        """首次成功，變更同一圖片src之後回傳HTTP失敗。"""
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(content_type="image/png", body=workflow.PREVIEW_BYTES)
        else:
            route.fulfill(status=502, body="synthetic failure")

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/late/preview*", preview)
    page.goto(base_url)
    page.locator("#post-url").fill("https://x.com/creator/status/1")
    page.locator("#analyze-button").click()
    page.wait_for_function(
        "/** 等待圖片真正成功解碼。 */ () => "
        "document.querySelector('.media-visual img')?.naturalWidth > 0"
    )
    page.locator(".media-selection").check()
    page.locator(".media-visual img").evaluate(
        "/** 只變更來源觸發真實error，不直接dispatch合成事件。 */ el => { el.src += '?late=1'; }"
    )
    expect(page.locator(".fallback-tile")).to_be_visible()
    expect(page.locator(".media-selection")).to_be_checked()
    expect(page.locator(".media-metadata")).to_have_count(1)
    assert len(calls) == 2
