"""驗證原生 dialog 的預設鍵盤路徑及既有背景隔離。"""

import pytest
from playwright.sync_api import Page, Route, expect

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


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("stage", ["HEAD", "GET"])
def test_pending_original_keeps_native_tab_and_escape_cleanup(
    engine_page: Page, base_url: str, stage: str
) -> None:
    """等待授權或原圖時不攔Tab；Esc取消signal且晚到回應不重建媒體。"""
    page = engine_page
    calls = viewer_workflow.open_results(page, base_url)
    held: list[Route] = []

    def hold(route: Route) -> None:
        """只延後目標網路階段，不替換真正fetch或媒體載入生命週期。"""
        if route.request.method == stage:
            held.append(route)
            page.evaluate(
                "/** 標示受控網路邊界已到達。 */ () => { window.originalWaiting = true; }"
            )
        else:
            route.fulfill(status=204)

    page.route("**/api/media/first/view", hold)
    page.evaluate(
        """/** 保留真正fetch，只觀察傳入的取消signal與鍵盤事件。 */ () => {
          const nativeFetch = window.fetch;
          window.fetch = /** 透傳原始請求並保存signal供取消驗證。 */ (url, options) => {
            if (String(url).endsWith('/view')) window.originalSignal = options.signal;
            return nativeFetch(url, options);
          };
          document.querySelector('#media-viewer').addEventListener('keydown',
            /** 在產品handler之後觀察是否攔截原生Tab。 */ event => {
              if (event.key === 'Tab') window.pendingTabPrevented = event.defaultPrevented;
            });
        }"""
    )
    opener = page.locator(".media-view").first
    opener.click()
    page.wait_for_function(
        "/** 等待網路真正進入指定階段。 */ () => window.originalWaiting === true"
    )
    assert len(held) == 1
    expect(page.locator("#viewer-message")).to_contain_text("正在載入")
    page.locator("#viewer-close").press("Tab")
    expect(page.locator("#viewer-next")).to_be_focused()
    assert page.evaluate("/** 原生Tab未被取消。 */ () => window.pendingTabPrevented") is False
    page.locator("#post-url").evaluate("/** 背景仍由原生modal隔離。 */ el => el.focus()")
    expect(page.locator("#viewer-next")).to_be_focused()
    if stage == "GET":
        page.locator("#viewer-content img").evaluate(
            "/** 保留原節點及晚到callback，驗證清理不依賴成功載入。 */ node => {"
            " window.waitingImage = node; window.waitingLoad = node.onload; }"
        )
    page.keyboard.press("Escape")
    expect(page.locator("#media-viewer")).to_be_hidden()
    expect(opener).to_be_focused()
    assert page.evaluate(
        "/** Esc必須取消真正fetch的signal。 */ () => window.originalSignal.aborted"
    )
    held[0].fulfill(status=204 if stage == "HEAD" else 200, body=b"")
    if stage == "GET":
        page.evaluate("/** 主動送出保存的晚到成功callback。 */ () => window.waitingLoad()")
        assert page.evaluate(
            "/** 已釋放圖片不能仍帶來源或留在文件中。 */ "
            "() => !window.waitingImage.isConnected && !window.waitingImage.hasAttribute('src')"
        )
    expect(page.locator("#viewer-content img, #viewer-content video")).to_have_count(0)
    expect(page.locator("#viewer-size")).to_be_disabled()
    assert page.evaluate("/** 清理後還原背景捲動。 */ () => document.body.style.overflow") == ""
    assert calls == []


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_escape_stops_playing_original_video(engine_page: Page, base_url: str) -> None:
    """真正播放後以Esc關閉，舊播放器必須暫停、解除來源且不載入下一則。"""
    page = engine_page
    calls = viewer_workflow.open_results(page, base_url, video=True)
    opener = page.locator(".media-view").first
    opener.click()
    page.locator("#viewer-play").click()
    page.wait_for_function(
        "/** 先證明真正解碼且播放時間已前進。 */ "
        "() => document.querySelector('#viewer-content video')?.currentTime > 0.1"
    )
    assert page.locator("#viewer-content video").evaluate(
        "/** 保存真正播放中的節點而不是替身。 */ node => {"
        " window.escapedVideo = node; return !node.paused && node.videoWidth === 320; }"
    )
    page.keyboard.press("Escape")
    expect(page.locator("#media-viewer")).to_be_hidden()
    expect(opener).to_be_focused()
    assert page.evaluate(
        "/** 關閉後播放器不可繼續播放或保留來源。 */ () => "
        "window.escapedVideo.paused && !window.escapedVideo.hasAttribute('src')"
        " && !window.escapedVideo.isConnected"
    )
    expect(page.locator("#viewer-content video")).to_have_count(0)
    assert all("/first/view" in url for _, url in calls)
    assert any(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_viewer_checkbox_arrows_do_not_navigate(engine_page: Page, base_url: str) -> None:
    """checkbox接收左右鍵不切媒體也不失焦，Space仍同步卡片選取。"""
    page = engine_page
    viewer_workflow.open_results(page, base_url)
    page.locator(".media-view").first.click()
    selected = page.locator("#viewer-selected")
    selected.focus()
    selected.press("ArrowRight")
    expect(page.locator("#viewer-position")).to_have_text("1 / 2")
    selected.press("ArrowLeft")
    expect(page.locator("#viewer-position")).to_have_text("1 / 2")
    expect(selected).to_be_focused()
    expect(selected).not_to_be_checked()
    selected.press("Space")
    expect(selected).to_be_checked()
    expect(page.locator(".media-selection").first).to_be_checked()
    expect(page.locator(".media-selection").nth(1)).not_to_be_checked()
