"""驗證原生 dialog 的預設鍵盤路徑及既有背景隔離。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_tab_defaults_to_native_modal_navigation(
    engine_page: Page, base_url: str, video: bool, reverse: bool
) -> None:
    """實際首尾 Tab 不被取消；背景不能聚焦，關閉後仍返回有效入口。"""
    page = engine_page
    calls = viewer_workflow.open_results(page, base_url, video=video)
    opener = page.locator(".media-view").first
    opener.click()
    last_control = page.locator("#viewer-play" if video else "#viewer-size")
    expect(last_control).to_be_enabled()
    expect(page.locator("#viewer-close")).to_be_focused()
    assert page.locator("#media-viewer").evaluate(
        "/** 確認使用真正的原生 modal，而非只加 open 屬性。 */ el => el.matches(':modal')"
    )
    page.locator("#post-url").evaluate(
        "/** 嘗試程式化聚焦背景，瀏覽器應由原生 inert 阻擋。 */ el => el.focus()"
    )
    expect(page.locator("#post-url")).not_to_be_focused()
    page.evaluate(
        """/** 觀察既有 handler 執行後的真實鍵盤事件，不替換任何事件或焦點API。 */ () => {
          window.viewerTabPrevented = null;
          document.querySelector('#media-viewer').addEventListener('keydown',
            /** 原生 Tab 的預設行為必須留給瀏覽器。 */
            event => {
              if (event.key === 'Tab') window.viewerTabPrevented = event.defaultPrevented;
            });
        }"""
    )
    (page.locator("#viewer-close") if reverse else last_control).focus()
    page.keyboard.press("Shift+Tab" if reverse else "Tab")
    assert (
        page.evaluate(
            "/** 讀取真正按鍵的預設動作是否被應用程式取消。 */ () => window.viewerTabPrevented"
        )
        is False
    )
    assert page.evaluate(
        """/** 瀏覽器工具列可能讓 activeElement 回 body，但背景控制永不獲焦點。 */ () => {
          const target = document.activeElement;
          return target === document.body || Boolean(target.closest('#media-viewer'));
        }"""
    )
    page.locator("#post-url").evaluate(
        "/** Tab 到瀏覽器介面後仍不可取得網頁背景控制。 */ el => el.focus()"
    )
    expect(page.locator("#post-url")).not_to_be_focused()
    if video:
        assert [method for method, _ in calls] == ["HEAD"]
        assert page.locator("#viewer-content video").get_attribute("src") is None
    # 從瀏覽器介面回到有效關閉控制後，Esc 仍沿用同一清理路徑。
    page.locator("#viewer-close").focus()
    page.keyboard.press("Escape")
    expect(page.locator("#media-viewer")).to_be_hidden()
    expect(opener).to_be_focused()
    expect(page.locator("#viewer-content img, #viewer-content video")).to_have_count(0)
    assert (
        page.evaluate("/** 關閉後還原既有背景捲動狀態。 */ () => document.body.style.overflow")
        == ""
    )


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width,height", [(320, 640), (800, 375)])
def test_modal_and_backdrop_are_scroll_containers(
    engine_page: Page, base_url: str, width: int, height: int
) -> None:
    """檢視器與遮罩各自隔離捲動，保留既有 body 鎖定及窄橫版操作。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": height})
    viewer_workflow.open_results(page, base_url)
    page.locator(".media-view").first.click()
    dialog = page.locator("#media-viewer")
    expect(dialog).to_have_css("overscroll-behavior", "contain")
    backdrop = dialog.evaluate(
        """/** 讀取實際 backdrop computed style，包含是否建立 scroll container。 */ el => {
          const style = getComputedStyle(el, '::backdrop');
          return [style.overflowX, style.overflowY, style.overscrollBehaviorX,
            style.overscrollBehaviorY];
        }"""
    )
    assert backdrop == ["hidden", "hidden", "contain", "contain"]
    assert (
        page.evaluate(
            "/** 不因 CSS 補強移除原有背景鎖定保障。 */ () => document.body.style.overflow"
        )
        == "hidden"
    )
    expect(page.locator("#viewer-close")).to_be_in_viewport()
    expect(page.locator("#viewer-next")).to_be_in_viewport()
    page.locator("#viewer-close").click()
    expect(dialog).to_be_hidden()
