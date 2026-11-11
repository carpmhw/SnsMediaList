"""以真正瀏覽器驗證原始媒體檢視與群組選取。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route, expect, sync_playwright

from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_playwright_workflow import SUCCESS_PAYLOAD
from tests.media_fixtures import original_png

page = workflow.page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.fixture(params=["chromium", "firefox", "webkit"])
def engine_page(request: pytest.FixtureRequest) -> Iterator[Page]:
    """在已安裝引擎執行真實播放，缺少 runtime 明確 skip 而不偽稱通過。"""
    with sync_playwright() as playwright:
        engine = {
            "chromium": playwright.chromium,
            "firefox": playwright.firefox,
            "webkit": playwright.webkit,
        }[request.param]
        try:
            browser = engine.launch(headless=True)
        except PlaywrightError as error:
            pytest.skip(f"{request.param} runtime 不可用：{error}")
        tab = browser.new_page(accept_downloads=True)
        try:
            yield tab
        finally:
            browser.close()


def open_results(
    page: Page, base_url: str, *, urls: list[str] | None = None, video: bool = False
) -> list[tuple[str, str]]:
    """掛入安全 API fixtures，回傳可驗證的原始媒體請求紀錄。"""
    urls = urls or ["/api/media/first/download", "/api/media/second/download"]
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {
                **SUCCESS_PAYLOAD["media"][0],
                "filename": f"video-{index + 1}.webm" if video else f"image-{index + 1}.png",
                "media_type": "video" if video else "image",
                "download_url": url,
            }
            for index, url in enumerate(urls)
        ],
    }
    calls: list[tuple[str, str]] = []

    def extraction(route: Route) -> None:
        """每次分析回傳相同的合成群組，避免外部平台依賴。"""
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    def view(route: Route) -> None:
        """HEAD 僅驗授權，GET 回傳真正可解碼原圖。"""
        calls.append((route.request.method, route.request.url))
        if route.request.method == "HEAD":
            route.fulfill(status=204)
        else:
            body = (
                (Path(__file__).parents[1] / "fixtures/viewer-video.webm").read_bytes()
                if video
                else original_png()
            )
            range_header = route.request.headers.get("range") if video else None
            if range_header:
                first, last = range_header.removeprefix("bytes=").split("-")
                start = int(first)
                end = min(int(last), len(body) - 1) if last else len(body) - 1
                route.fulfill(
                    status=206,
                    content_type="video/webm",
                    body=body[start : end + 1],
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{len(body)}",
                        "Accept-Ranges": "bytes",
                    },
                )
            else:
                route.fulfill(content_type="video/webm" if video else "image/png", body=body)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/*/view", view)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    expect(page.locator(".media-card")).to_have_count(len(urls))
    return calls


def test_viewer_opens_original_and_keeps_card_actions_separate(page: Page, base_url: str) -> None:
    """檢視入口定位正確原圖，勾選與複製不會誤開 dialog。"""
    calls = open_results(page, base_url)
    page.locator(".media-selection").first.check()
    expect(page.locator("dialog[open]")).to_have_count(0)
    page.locator(".media-view").nth(1).click(timeout=3000)
    dialog = page.get_by_role("dialog", name="原始媒體檢視")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#viewer-filename")).to_have_text("image-2.png")
    expect(dialog.locator("#viewer-position")).to_have_text("2 / 2")
    page.wait_for_function("document.querySelector('#viewer-content img')?.naturalWidth === 1600")
    assert [method for method, _ in calls] == ["HEAD", "GET"]
    assert all("/second/view" in url for _, url in calls)
    page.click("#viewer-close")
    expect(page.locator(".media-view").nth(1)).to_be_focused()
    expect(page.locator(".media-selection").first).to_be_checked()


def test_viewer_navigation_and_selection_share_group_state(page: Page, base_url: str) -> None:
    """同群組來源順序與共享選取在關閉後仍一致。"""
    open_results(page, base_url)
    page.locator(".media-view").first.click(timeout=3000)
    expect(page.locator("#viewer-previous")).to_be_disabled()
    page.locator("#viewer-selected").check()
    page.click("#viewer-next")
    expect(page.locator("#viewer-position")).to_have_text("2 / 2")
    expect(page.locator("#viewer-selected")).not_to_be_checked()
    expect(page.locator("#viewer-next")).to_be_disabled()
    page.locator("#viewer-selected").check()
    page.keyboard.press("Escape")
    expect(page.locator(".selection-summary")).to_have_text("2 / 2 已選取")
    expect(page.locator(".media-selection").nth(1)).to_be_checked()


def test_viewer_single_item_and_independent_groups(page: Page, base_url: str) -> None:
    """單項停用兩端，批次群組的選取與下載按鈕互不污染。"""
    calls = open_results(page, base_url, urls=["/api/media/first/download"])
    page.click("#batch-mode-button")
    page.fill("#batch-post-urls", "https://x.com/creator/status/2")
    page.click("#analyze-batch-button")
    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.nth(0).locator(".media-view").click()
    expect(page.locator("#viewer-position")).to_have_text("1 / 1")
    expect(page.locator("#viewer-previous")).to_be_disabled()
    expect(page.locator("#viewer-next")).to_be_disabled()
    page.locator("#viewer-selected").check()
    page.click("#viewer-close")
    expect(groups.nth(0).locator(".selection-summary")).to_have_text("1 / 1 已選取")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("0 / 1 已選取")
    page.click("#next-result-group")
    groups.nth(1).locator(".media-view").click()
    expect(page.locator("#viewer-selected")).not_to_be_checked()
    page.click("#viewer-close")
    expect(groups.nth(0).get_by_role("button", name="下載選取項目")).to_be_enabled()
    expect(groups.nth(1).get_by_role("button", name="下載選取項目")).to_be_disabled()
    assert not any("download" in url for _, url in calls)


def test_card_copy_and_download_do_not_open_viewer(page: Page, base_url: str) -> None:
    """複製與原生下載仍使用各自入口，不觸發 viewer 或其 HEAD。"""
    calls = open_results(page, base_url, urls=["/api/media/first/download"])

    def download(route: Route) -> None:
        """只提供原有下載流程的 HEAD 與 attachment fixture。"""
        if route.request.method == "HEAD":
            route.fulfill(status=204)
        else:
            route.fulfill(
                body=original_png(),
                content_type="image/png",
                headers={
                    "Content-Disposition": 'attachment; filename="image.png"',
                },
            )

    page.route("**/api/media/first/download", download)
    page.locator(".copy-filename").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    with page.expect_download():
        page.locator(".download-action").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert calls == []


def test_viewer_actual_size_and_reset(page: Page, base_url: str) -> None:
    """原始大小依自然尺寸顯示且只捲動媒體區，換項目回到 fit。"""
    open_results(page, base_url)
    page.locator(".media-view").first.click(timeout=3000)
    image = page.locator("#viewer-content img")
    page.wait_for_function("document.querySelector('#viewer-content img')?.naturalWidth === 1600")
    assert image.evaluate("img => img.getBoundingClientRect().width") < 1600
    page.click("#viewer-size")
    assert image.evaluate("img => img.getBoundingClientRect().width") == 1600
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#viewer-content").evaluate(
        "element => { element.scrollLeft = 100; element.scrollTop = 100; }"
    )
    page.click("#viewer-next")
    expect(page.locator("#viewer-size")).to_have_text("原始大小")
    assert page.locator("#viewer-content").evaluate(
        "element => [element.scrollLeft, element.scrollTop]"
    ) == [0, 0]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api/media/token/download",
        "//evil.example/a/download",
        "/api/media/token/download?query=1",
        "/api/media/token/download#fragment",
        "/api/media/token/preview",
        "/api/media/token%2Fextra/download",
        "/api/media/token/download\n",
    ],
)
def test_viewer_rejects_nonlocal_or_noncanonical_url(page: Page, base_url: str, url: str) -> None:
    """檢視來源不能由外部 URL 或不完整 download path 推導。"""
    calls = open_results(page, base_url, urls=[url])
    page.locator(".media-view").click()
    expect(page.locator("#viewer-message")).to_contain_text("目前無法檢視")
    assert calls == []
    expect(page.locator("#viewer-content img")).to_have_count(0)


@pytest.mark.parametrize("status", [404, 410, 429, 502])
def test_viewer_head_failure_never_loads_original(page: Page, base_url: str, status: int) -> None:
    """HEAD 失敗不發 GET，不顯示來源 body，也不自動重新擷取。"""
    open_results(page, base_url)
    methods: list[str] = []

    def fail(route: Route) -> None:
        """回傳固定狀態與不可直接顯示的合成診斷。"""
        methods.append(route.request.method)
        route.fulfill(status=status, body="PRIVATE_URL_TOKEN_SENTINEL")

    page.route("**/api/media/*/view", fail)
    page.locator(".media-view").first.click()
    expect(page.locator("#viewer-message")).not_to_contain_text("正在載入")
    assert methods == ["HEAD"]
    expect(page.locator("#viewer-content img")).to_have_count(0)
    expect(page.locator("#media-viewer")).not_to_contain_text("SENTINEL")
    expect(page.locator("#viewer-recovery")).to_be_visible() if status in {404, 410} else expect(
        page.locator("#viewer-recovery")
    ).to_be_hidden()


@pytest.mark.parametrize(
    "width,height", [(320, 640), (375, 800), (768, 1024), (1280, 900), (800, 375)]
)
def test_viewer_fits_viewport_and_keyboard(
    page: Page, base_url: str, width: int, height: int
) -> None:
    """直橫向螢幕原圖保持比例，操作列不被媒體推到視窗外。"""
    page.set_viewport_size({"width": width, "height": height})
    open_results(page, base_url)
    page.locator(".media-view").first.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#viewer-content img')?.naturalWidth === 1600")
    expect(page.locator("#viewer-close")).to_be_focused()
    expect(page.locator("#viewer-next")).to_be_in_viewport()
    expect(page.locator("#viewer-size")).to_be_in_viewport()
    bounds = page.locator("#viewer-content").bounding_box()
    image = page.locator("#viewer-content img").bounding_box()
    assert bounds and image
    assert image["height"] <= bounds["height"] + 1
    assert image["width"] <= bounds["width"] + 1
    assert abs(image["width"] / image["height"] - 1.6) < 0.02
    for _ in range(8):
        page.keyboard.press("Tab")
        assert page.evaluate("Boolean(document.activeElement.closest('#media-viewer'))")
    page.locator("#viewer-close").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#viewer-position")).to_have_text("2 / 2")
    page.keyboard.press("Escape")
    expect(page.locator(".media-view").first).to_be_focused()


def test_viewer_backdrop_focus_fallback_and_long_name(page: Page, base_url: str) -> None:
    """長檔名不推走控制，背景 inert，觸發入口移除後仍有有效焦點。"""
    page.set_viewport_size({"width": 320, "height": 640})
    page.emulate_media(reduced_motion="reduce")
    open_results(page, base_url)
    page.locator(".media-view").first.click()
    page.wait_for_function("document.querySelector('#viewer-content img')?.naturalWidth === 1600")
    page.locator("#viewer-filename").evaluate(
        "node => { node.textContent = '很長的檔名'.repeat(80) + '.png'; }"
    )
    expect(page.locator("#viewer-close")).to_be_in_viewport()
    expect(page.locator("#viewer-next")).to_be_in_viewport()
    page.locator("#post-url").evaluate("node => node.focus()")
    assert page.evaluate("Boolean(document.activeElement.closest('#media-viewer'))")
    page.locator(".media-view").first.evaluate("node => node.remove()")
    page.keyboard.press("Escape")
    expect(page.locator(".media-view").first).to_be_focused()
    page.locator(".media-view").first.click()
    page.locator(".media-view").evaluate_all("nodes => nodes.forEach(node => node.remove())")
    page.mouse.click(1, 1)
    expect(page.locator("#media-viewer")).not_to_be_visible()
    expect(page.locator("#post-url")).to_be_focused()
    assert page.evaluate("document.body.style.overflow") == ""


def test_viewer_video_manual_play_and_seek(page: Page, base_url: str) -> None:
    """真正 WebM 手動開始後時間前進，原生控制可暫停並完成定位。"""
    calls = open_results(page, base_url, video=True)
    page.locator(".media-view").first.click()
    play = page.get_by_role("button", name="播放影片", exact=True)
    expect(play).to_be_enabled(timeout=3000)
    video = page.locator("#viewer-content video")
    expect(video).to_have_attribute("playsinline", "")
    assert video.get_attribute("src") is None
    assert [method for method, _ in calls] == ["HEAD"]
    play.click(timeout=3000)
    page.wait_for_function("document.querySelector('#viewer-content video').currentTime > 0.15")
    assert video.evaluate("node => !node.paused && node.videoWidth === 320")
    video.evaluate("node => { node.pause(); node.currentTime = 2; }")
    page.wait_for_function("!document.querySelector('#viewer-content video').seeking")
    assert abs(video.evaluate("node => node.currentTime") - 2) < 0.1
    assert video.evaluate("node => node.paused")
    assert any(method == "GET" for method, _ in calls)


@pytest.mark.parametrize(
    "action", ["next", "close", "pagehide", "replace-all", "replace-group", "group-switch"]
)
def test_viewer_teardown_stops_old_video(page: Page, base_url: str, action: str) -> None:
    """所有離開路徑都暫停、解除來源並阻止下一則自動載入。"""
    calls = open_results(page, base_url, video=True)
    if action == "replace-group":
        page.route(
            "**/api/media/*/download",
            lambda route: route.fulfill(
                status=410,
                content_type="application/json",
                body=json.dumps({"code": "token_expired"}),
            ),
        )
        page.locator(".download-action").first.click()
        expect(page.locator(".group-recovery .re-analyze")).to_be_visible()
    if action == "group-switch":
        page.click("#batch-mode-button")
        page.fill("#batch-post-urls", "https://x.com/creator/status/2")
        page.click("#analyze-batch-button")
        expect(page.locator(".result-group")).to_have_count(2)
    page.locator(".media-view").first.click()
    page.click("#viewer-play")
    page.wait_for_function("document.querySelector('#viewer-content video').currentTime > 0.1")
    page.locator("#viewer-content video").evaluate("node => { window.oldVideo = node; }")
    if action == "next":
        page.click("#viewer-next")
        expect(page.locator("#viewer-play")).to_be_enabled()
        assert page.locator("#viewer-content video").get_attribute("src") is None
        expect(page.locator("#viewer-position")).to_have_text("2 / 2")
    elif action == "close":
        page.click("#viewer-close")
    elif action == "pagehide":
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    elif action == "replace-all":
        page.evaluate("document.querySelector('#extraction-form').requestSubmit()")
    elif action == "replace-group":
        page.locator(".group-recovery .re-analyze").evaluate("button => button.click()")
    else:
        page.locator("#next-result-group").evaluate("button => button.click()")
    if action != "next":
        expect(page.locator("#media-viewer")).not_to_be_visible()
        assert page.evaluate("document.body.style.overflow") == ""
    assert page.evaluate("oldVideo.paused && oldVideo.getAttribute('src') === null")
    assert all("/first/view" in url for method, url in calls if method == "GET")


def test_viewer_switch_aborts_head_and_ignores_late_response(page: Page, base_url: str) -> None:
    """切換會取消舊 HEAD，晚到成功不載入舊媒體或覆蓋新項目。"""
    calls = open_results(page, base_url)
    held: list[Route] = []

    def hold(route: Route) -> None:
        """延後第一項授權預檢，保留真實瀏覽器 fetch 與 signal。"""
        held.append(route)

    page.route("**/api/media/first/view", hold)
    page.evaluate("""() => {
      window.viewAborts = 0;
      const nativeFetch = window.fetch;
      // 只觀察傳入真正 fetch 的 AbortSignal，不取代網路或回應。
      window.fetch = (url, options) => {
        if (String(url).endsWith('/view')) {
          options.signal.addEventListener('abort', () => window.viewAborts++);
        }
        return nativeFetch(url, options);
      };
    }""")
    page.locator(".media-view").first.click()
    expect(page.locator("#viewer-message")).to_contain_text("正在載入")
    page.click("#viewer-next")
    page.wait_for_function("window.viewAborts === 1")
    expect(page.locator("#viewer-filename")).to_have_text("image-2.png")
    assert held
    held[0].fulfill(status=204)
    page.wait_for_function("document.querySelector('#viewer-content img')?.naturalWidth === 1600")
    expect(page.locator("#viewer-filename")).to_have_text("image-2.png")
    assert all("/second/view" in url for method, url in calls if method == "GET")


def test_viewer_late_media_events_and_play_promise_are_isolated(page: Page, base_url: str) -> None:
    """舊影片事件與延後拒絕的 play promise 不恢復播放或覆寫新選取。"""
    calls = open_results(page, base_url, video=True)
    page.evaluate("""() => {
      const originalPlay = HTMLMediaElement.prototype.play;
      // 保留真正播放副作用，只控制回報給 caller 的 promise 完成時間。
      HTMLMediaElement.prototype.play = function () {
        originalPlay.call(this).catch(() => {});
        return new Promise((resolve, reject) => { window.rejectOldPlay = reject; });
      };
    }""")
    page.locator(".media-view").first.click()
    page.click("#viewer-play")
    page.locator("#viewer-content video").evaluate("""node => {
      window.oldVideo = node; window.lateLoad = node.onloadeddata; window.lateError = node.onerror;
    }""")
    page.click("#viewer-next")
    expect(page.locator("#viewer-play")).to_be_enabled()
    page.locator("#viewer-selected").check()
    focused = page.evaluate("document.activeElement.id")
    page.evaluate("""async () => {
      window.rejectOldPlay(new Error('PRIVATE_PLAY_SENTINEL'));
      window.lateLoad(); await window.lateError();
    }""")
    expect(page.locator("#viewer-filename")).to_have_text("video-2.webm")
    expect(page.locator("#viewer-selected")).to_be_checked()
    expect(page.locator("#viewer-message")).to_contain_text("按「播放影片」")
    assert page.evaluate("document.activeElement.id") == focused
    assert page.evaluate("oldVideo.paused && oldVideo.getAttribute('src') === null")
    assert not any(method == "GET" and "/second/" in url for method, url in calls)


def test_viewer_video_arrow_keys_do_not_navigate_items(page: Page, base_url: str) -> None:
    """影片控制接收方向鍵時，不被檢視器左右導覽攔截。"""
    open_results(page, base_url, video=True)
    page.locator(".media-view").first.click()
    page.click("#viewer-play")
    page.wait_for_function("document.querySelector('#viewer-content video').currentTime > 0.1")
    video = page.locator("#viewer-content video")
    video.focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#viewer-position")).to_have_text("1 / 2")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#viewer-position")).to_have_text("1 / 2")


@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize("status", [204, 404, 410, 429])
def test_viewer_media_error_only_rechecks_token_once(
    page: Page,
    base_url: str,
    video: bool,
    status: int,
) -> None:
    """格式／定位失敗僅補一次 HEAD，不重送媒體或自動推斷原因。"""
    open_results(page, base_url, video=video)
    methods: list[str] = []

    def invalid_media(route: Route) -> None:
        """先讓授權通過，再讓原始媒體回應安全失敗。"""
        methods.append(route.request.method)
        if route.request.method == "HEAD":
            route.fulfill(status=204 if len(methods) == 1 else status)
        else:
            route.fulfill(status=502, body="PRIVATE_URL_TOKEN_SENTINEL")

    page.route("**/api/media/*/view", invalid_media)
    page.locator(".media-view").first.click()
    if video:
        page.click("#viewer-play")
    expect(page.locator("#viewer-message")).to_contain_text(
        "參照已不可用"
        if status in {404, 410}
        else "服務目前忙碌"
        if status == 429
        else "目前無法檢視",
    )
    assert methods == ["HEAD", "GET", "HEAD"]
    page.locator("#viewer-content img, #viewer-content video").dispatch_event("error")
    assert methods == ["HEAD", "GET", "HEAD"]
    expect(page.locator("#media-viewer")).not_to_contain_text("SENTINEL")


def test_viewer_play_rejection_is_safe(page: Page, base_url: str) -> None:
    """play promise 失敗呈現固定訊息且不自動重試或下載。"""
    calls = open_results(page, base_url, video=True)
    page.evaluate("""() => {
      // 模擬瀏覽器播放政策拒絕，並保留其餘真實媒體生命週期。
      HTMLMediaElement.prototype.play = function () {
        return Promise.reject(new Error('PRIVATE_CODEC_SENTINEL'));
      };
    }""")
    page.locator(".media-view").first.click()
    page.click("#viewer-play")
    expect(page.locator("#viewer-message")).to_contain_text("目前無法檢視")
    expect(page.locator("#media-viewer")).not_to_contain_text("SENTINEL")
    assert [method for method, _ in calls].count("HEAD") == 2
    assert [method for method, _ in calls].count("GET") <= 1
    assert page.locator("#viewer-content video").get_attribute("src") is None


@pytest.mark.parametrize("outcome", ["success", "abort"])
def test_viewer_recovery_is_explicit_and_group_local(
    page: Page, base_url: str, outcome: str
) -> None:
    """失效復原先關閉，晚到結果只替換原群組，取消不重新開啟。"""
    open_results(page, base_url)
    page.click("#batch-mode-button")
    page.fill("#batch-post-urls", "https://x.com/creator/status/2")
    page.click("#analyze-batch-button")
    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    for index in (0, 1):
        groups.nth(index).locator(".media-selection").first.evaluate("node => node.click()")
    held: list[Route] = []

    def expired(route: Route) -> None:
        """只回失效授權，任何 GET 都視為測試失敗。"""
        assert route.request.method == "HEAD"
        route.fulfill(status=410)

    def recover(route: Route) -> None:
        """保留重新分析回應，以驗證 viewer 關閉早於擷取完成。"""
        held.append(route)

    page.route("**/api/media/*/view", expired)
    page.route("**/api/extractions", recover)
    groups.nth(0).locator(".media-view").nth(1).click()
    expect(page.locator("#viewer-recovery")).to_be_visible()
    assert held == []
    page.click("#viewer-recovery")
    expect(page.locator("#media-viewer")).not_to_be_visible()
    expect(groups.nth(0).locator(".group-status")).to_contain_text("正在重新分析")
    assert held
    if outcome == "success":
        held[0].fulfill(content_type="application/json", body=json.dumps(SUCCESS_PAYLOAD))
        expect(groups.nth(0).locator(".media-card")).to_have_count(1)
        expect(groups.nth(0).locator(".selection-summary")).to_have_text("0 / 1 已選取")
    else:
        held[0].abort()
        expect(groups.nth(0).locator(".group-status")).to_contain_text("失敗")
    expect(groups.nth(1).locator(".selection-summary")).to_have_text("1 / 2 已選取")
    expect(page.locator("#media-viewer")).not_to_be_visible()


def test_viewer_available_engines(engine_page: Page, base_url: str) -> None:
    """各引擎驗證真正播放／seek 及手機尺寸的鍵盤與焦點契約。"""
    test_viewer_video_manual_play_and_seek(engine_page, base_url)
    test_viewer_fits_viewport_and_keyboard(engine_page, base_url, 375, 800)
