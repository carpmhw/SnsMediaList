"""以瀏覽器真實介面驗證資料夾下載；picker／磁碟邊界使用明確測試替身。"""

import json

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_playwright_workflow as workflow

SUCCESS_PAYLOAD = workflow.SUCCESS_PAYLOAD
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods
page = workflow.page

PICKER_SETUP = """
/** 安裝可控制的 picker，保留真實瀏覽器 fetch 與串流能力。 */
() => {
  window.pickerCalls = 0;
  window.pickerActivation = false;
  /** 記錄使用者啟動狀態並等待測試選擇或拒絕。 */
  window.showDirectoryPicker = function () {
    window.pickerCalls += 1;
    window.pickerActivation = navigator.userActivation.isActive;
    /** 將 picker 的完成權交給測試，不啟動系統對話框。 */
    return new Promise(function (resolve, reject) {
      window.acceptPicker = resolve;
      window.rejectPicker = reject;
    });
  };
}
"""


def prepare(page: Page, base_url: str, *, count: int = 2, same_name: bool = False) -> list[str]:
    """載入真實靜態頁面，僅替換分析 API 與媒體 HTTP 回應。"""
    requests: list[str] = []
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {
                **SUCCESS_PAYLOAD["media"][0],
                "filename": "same.jpg" if same_name else f"media-{index}.jpg",
                "download_url": f"/api/media/item-{index}/download",
            }
            for index in range(count)
        ],
    }

    def extraction(route: Route) -> None:
        """傳回不含真實平台資料的完整合成結果。"""
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    def media(route: Route) -> None:
        """記錄實際 HEAD／GET 並提供可辨認的內容。"""
        requests.append(route.request.method)
        route.fulfill(
            status=204 if route.request.method == "HEAD" else 200,
            content_type="image/jpeg",
            body=b"" if route.request.method == "HEAD" else b"fixture-bytes",
        )

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/download", media)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    expect(page.locator(".media-selection")).to_have_count(count)
    page.get_by_role("button", name="全選", exact=True).click()
    return requests


@pytest.mark.parametrize("error_name", ["AbortError", "NotAllowedError", "SecurityError"])
def test_picker_rejection_never_falls_back(page: Page, base_url: str, error_name: str) -> None:
    """捕捉誤用原生流程：取消／拒絕 picker 後不能出現任何媒體請求。"""
    page.add_init_script(f"({PICKER_SETUP})();")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("選擇資料夾")
    assert page.evaluate("window.pickerCalls") == 1
    assert page.evaluate("window.pickerActivation") is True
    assert requests == []
    page.evaluate("name => window.rejectPicker(new DOMException('private-path', name))", error_name)
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    assert requests == []
    assert "private-path" not in page.locator("#results").inner_text()


@pytest.mark.parametrize(
    "missing",
    ["picker", "writer", "stream", "response", "abort", "secure"],
)
def test_missing_capability_uses_native_download(page: Page, base_url: str, missing: str) -> None:
    """捕捉品牌或不完整能力判斷：任何必要能力不足都需保留原生下載。"""
    setup = {
        "picker": "window.showDirectoryPicker = undefined;",
        "writer": "FileSystemFileHandle.prototype.createWritable = undefined;",
        "stream": "window.ReadableStream = undefined;",
        "response": "Object.defineProperty(Response.prototype, 'body', {value: undefined});",
        "abort": "window.AbortController = undefined;",
        "secure": "Object.defineProperty(window, 'isSecureContext', {value: false});",
    }
    page.add_init_script(f"({PICKER_SETUP})(); {setup[missing]}")
    requests = prepare(page, base_url, count=1)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("1 個下載已開始")
    assert page.evaluate("window.pickerCalls") == 0
    assert requests[0] == "HEAD"


def test_pending_picker_locks_run_and_snapshot(page: Page, base_url: str) -> None:
    """捕捉勾選解除鎖定或重複點擊造成兩批下載。"""
    page.add_init_script(f"({PICKER_SETUP})();")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("選擇資料夾")
    page.locator(".media-selection").first.uncheck()
    expect(page.get_by_role("button", name="下載選取項目")).to_be_disabled()
    page.get_by_role("button", name="下載選取項目").dispatch_event("click")
    assert page.evaluate("window.pickerCalls") == 1
    assert requests == []


SINK_SETUP = """
/** 建立可觀察 close／abort 的磁碟替身，不模擬手機系統授權介面。 */
(options) => {
  window.files = {};
  window.diskEvents = [];
  /** 讓指定操作可由測試延後完成或注入固定失敗。 */
  async function gate(operation) {
    if (options.fail === operation) throw new DOMException('private-path', 'NotAllowedError');
    if (options.hold === operation) {
      options.hold = null;
      /** 將磁碟操作的完成權交給測試。 */
      await new Promise(function (resolve) { window.releaseDisk = resolve; });
    }
  }
  const directory = {
    /** 查詢或建立單層檔案，保留既有內容供非覆寫斷言。 */
    async getFileHandle(name, settings = {}) {
      if (options.queryError) throw new DOMException('private-path', 'NotAllowedError');
      if (options.existingDirectory === name) throw new DOMException('', 'TypeMismatchError');
      if (!(name in window.files)) {
        if (!settings.create) throw new DOMException('', 'NotFoundError');
        window.files[name] = {bytes: [], closed: false, aborted: false};
      }
      const file = window.files[name];
      return {
        /** 建立只在 close 後提交 bytes 的 writable 替身。 */
        async createWritable() {
          await gate('create');
          const pending = [];
          return {
            /** 複製收到的 bytes 以驗證產品實際交付內容。 */
            async write(chunk) {
              window.diskEvents.push('write');
              await gate('write');
              pending.push(...chunk);
            },
            /** 模擬關閉成功才提交內容。 */
            async close() {
              window.diskEvents.push('close');
              await gate('close');
              file.bytes = pending;
              file.closed = true;
            },
            /** 中止不提交部分內容。 */
            async abort() {
              window.diskEvents.push('abort');
              await gate('abort');
              file.aborted = true;
            },
          };
        },
      };
    },
  };
  if (options.existing) window.files[options.existing] = {bytes: [99], closed: true};
  window.testDirectory = directory;
  /** 直接回傳資料夾替身，實際 picker 的取消由另外的測試覆蓋。 */
  window.showDirectoryPicker = async function () { return directory; };
}
"""


def install_sink(page: Page, **options: object) -> None:
    """在頁面載入前安裝可控磁碟邊界。"""
    page.add_init_script(f"({SINK_SETUP})({json.dumps(options)});")


@pytest.mark.parametrize("known", [True, False])
def test_folder_writes_exact_body_and_waits_for_close(
    page: Page, base_url: str, known: bool
) -> None:
    """捕捉寫完即宣告成功、未等 close 或保存內容不完整。"""
    install_sink(page, hold="close")
    requests = prepare(page, base_url, count=1)
    if not known:
        page.route(
            "**/api/media/**/download",
            lambda route: route.fulfill(
                status=204 if route.request.method == "HEAD" else 200,
                body=b"" if route.request.method == "HEAD" else b"fixture-bytes",
                headers={} if route.request.method == "HEAD" else {"Transfer-Encoding": "chunked"},
            ),
        )
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-detail")).to_contain_text("正在完成儲存")
    assert page.evaluate("Object.values(window.files)[0].closed") is False
    assert "已儲存 1" not in page.locator(".download-summary").inner_text()
    page.evaluate("window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
    assert page.evaluate("window.files['media-0.jpg'].bytes") == list(b"fixture-bytes")
    if known:
        assert requests == ["HEAD", "GET"]


@pytest.mark.parametrize("operation", ["create", "write", "close"])
def test_disk_failure_stops_batch_without_fallback(
    page: Page, base_url: str, operation: str
) -> None:
    """捕捉磁碟錯誤後仍下載下一項或改用原生下載。"""
    install_sink(page, fail=operation)
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    expect(page.locator(".download-summary")).to_contain_text("失敗 1")
    assert requests == ["HEAD", "GET"]
    assert "private-path" not in page.locator("#results").inner_text()
    assert not page.evaluate("Object.values(window.files).some(file => file.closed)")


def controlled_stream(page: Page, *, failure: bool = False, length: str | None = "4") -> None:
    """替換外部 GET 邊界，使用真實零預取 ReadableStream 驗證讀寫背壓與中斷。"""
    page.evaluate(
        """/** 提供兩段已知內容並記錄實際讀取次數。 */
        ({failure, length}) => {
          const original = window.fetch;
          window.streamReads = 0;
          window.streamCancelled = false;
          /** 只替換媒體 GET；HEAD 仍通過瀏覽器實際 HTTP 路徑。 */
          window.fetch = async function (url, settings = {}) {
            if (!String(url).endsWith('/download') || settings.method === 'HEAD') {
              return original(url, settings);
            }
            let index = 0;
            const stream = new ReadableStream({
              /** 每次 read 才提供下一段，第三次可模擬傳輸中斷。 */
              pull(controller) {
                window.streamReads += 1;
                if (index < 2) controller.enqueue(new Uint8Array([++index, index + 10]));
                else if (failure) controller.error(new TypeError('private-path'));
                else controller.close();
              },
              /** 觀察取消是否釋放受控的 response body。 */
              cancel() { window.streamCancelled = true; },
            }, {highWaterMark: 0});
            return new Response(stream, {
              headers: length === null ? {} : {'Content-Length': length},
            });
          };
        }""",
        {"failure": failure, "length": length},
    )


def test_stream_backpressure_preserves_exact_chunks(page: Page, base_url: str) -> None:
    """捕捉提前 read、漏寫或將全部 chunk 累積後才寫入。"""
    install_sink(page, hold="write")
    prepare(page, base_url, count=1)
    controlled_stream(page)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    assert page.evaluate("window.streamReads") == 1
    page.evaluate("window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
    assert page.evaluate("window.files['media-0.jpg'].bytes") == [1, 11, 2, 12]
    assert page.evaluate("window.diskEvents") == ["write", "write", "close"]


@pytest.mark.parametrize("failure,length", [(True, "4"), (False, "9")])
def test_failed_stream_aborts_without_committing(
    page: Page, base_url: str, failure: bool, length: str
) -> None:
    """捕捉錯誤或長度不足仍 close 並宣告成功。"""
    install_sink(page)
    prepare(page, base_url, count=1)
    controlled_stream(page, failure=failure, length=length)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("失敗 1")
    assert page.evaluate("window.files['media-0.jpg'].aborted") is True
    assert page.evaluate("window.files['media-0.jpg'].closed") is False
    assert page.evaluate("window.files['media-0.jpg'].bytes") == []


def test_folder_names_never_overwrite_existing_file_or_directory(page: Page, base_url: str) -> None:
    """捕捉重複命名直接覆寫及同名目錄被誤判為空名稱。"""
    install_sink(page, existing="media-0.jpg", existingDirectory="media-0 (1).jpg")
    prepare(page, base_url, count=1)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")
    assert page.evaluate("window.files['media-0.jpg'].bytes") == [99]
    assert page.evaluate("window.files['media-0 (2).jpg'].bytes") == list(b"fixture-bytes")


def test_directory_query_error_never_creates_or_overwrites(page: Page, base_url: str) -> None:
    """捕捉將權限或查詢錯誤誤判為不存在。"""
    install_sink(page, queryError=True)
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("失敗 1")
    assert page.evaluate("window.files") == {}
    assert requests == ["HEAD", "GET"]


def test_duplicate_names_within_batch_get_distinct_files(page: Page, base_url: str) -> None:
    """捕捉一批內重複名稱覆蓋前一項。"""
    install_sink(page)
    prepare(page, base_url, same_name=True)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("已儲存 2")
    assert page.evaluate("Object.keys(window.files)") == ["same.jpg", "same (1).jpg"]


@pytest.mark.parametrize("length", ["4", None, "0"])
def test_progress_is_truthful_and_accessible(page: Page, base_url: str, length: str | None) -> None:
    """捕捉未知長度仍假造百分比，或 bytes 已收齊就提早增加完成數。"""
    install_sink(page, hold="write")
    prepare(page, base_url, count=1)
    controlled_stream(page, length=length)
    page.get_by_role("button", name="下載選取項目").click()
    if length == "0":
        # 零長度卻有 body 是不一致，應拒絕而不是除以零或假造成功。
        expect(page.locator(".download-summary")).to_contain_text("失敗 1")
        assert "NaN" not in page.locator("#results").inner_text()
        return
    page.wait_for_function("window.diskEvents.includes('write')")
    progress = page.get_by_role("progressbar", name="目前檔案傳輸進度")
    expect(progress).to_be_visible()
    if length:
        expect(progress).to_have_attribute("value", "50")
        expect(page.locator(".download-detail")).to_contain_text("50%")
    else:
        expect(progress).not_to_have_attribute("value", "")
        assert progress.get_attribute("value") is None
        assert progress.get_attribute("aria-valuenow") is None
        assert "%" not in page.locator(".download-detail").inner_text()
    assert progress.get_attribute("aria-live") is None
    assert "2 bytes" in page.locator(".download-detail").inner_text()
    page.evaluate("window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1")


def test_stop_aborts_partial_file_and_skips_remaining(page: Page, base_url: str) -> None:
    """捕捉停止後仍 close、提交殘檔或繼續下一個媒體。"""
    install_sink(page, hold="write")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    page.get_by_role("button", name="停止下載", exact=True).click()
    page.evaluate("window.releaseDisk()")
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    expect(page.locator(".download-summary")).to_contain_text("已停止")
    assert requests == ["HEAD", "GET"]
    assert page.evaluate("window.files['media-0.jpg'].aborted") is True
    assert page.evaluate("window.files['media-0.jpg'].closed") is False


@pytest.mark.parametrize("invalidate", ["clear", "pagehide"])
def test_invalidated_run_does_not_write_new_results(
    page: Page, base_url: str, invalidate: str
) -> None:
    """捕捉離頁或新分析後舊下載仍提交內容／啟動下一項。"""
    install_sink(page, hold="write")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    if invalidate == "clear":
        page.fill("#post-url", "https://x.com/creator/status/2")
        page.click("#analyze-button")
        expect(page.locator(".selection-summary")).to_contain_text("0 / 2")
    else:
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.evaluate("window.releaseDisk()")
    page.wait_for_function("window.files['media-0.jpg'].aborted")
    assert requests == ["HEAD", "GET"]
    assert page.evaluate("window.files['media-0.jpg'].closed") is False
    if invalidate == "clear":
        expect(page.locator(".download-summary")).to_have_text("")


def test_late_picker_after_clear_never_starts_download(page: Page, base_url: str) -> None:
    """捕捉清空結果後 picker 晚回傳仍使用舊 token。"""
    page.add_init_script(f"({PICKER_SETUP})();")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("選擇資料夾")
    page.fill("#post-url", "https://x.com/creator/status/2")
    page.click("#analyze-button")
    expect(page.locator(".selection-summary")).to_contain_text("0 / 2")
    page.evaluate(SINK_SETUP, {})
    page.evaluate("window.acceptPicker(window.testDirectory)")
    expect(page.locator(".download-summary")).to_have_text("")
    assert requests == []
    assert page.evaluate("window.files") == {}


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_progress_controls_fit_mobile_and_keyboard(page: Page, base_url: str, width: int) -> None:
    """捕捉進度區或取消控制項在窄螢幕溢出與無法鍵盤停止。"""
    page.set_viewport_size({"width": width, "height": 900})
    install_sink(page, hold="write")
    prepare(page, base_url, count=1)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    stop = page.get_by_role("button", name="停止下載", exact=True)
    stop.focus()
    expect(stop).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".download-detail").get_attribute("aria-live") is None
    page.keyboard.press("Enter")
    page.evaluate("window.releaseDisk()")
    expect(page.locator(".download-summary")).to_contain_text("已停止")


@pytest.mark.parametrize("failure", ["expired", "http", "stream", "cleanup"])
def test_item_failures_continue_but_cleanup_failure_stops(
    page: Page, base_url: str, failure: str
) -> None:
    """捕捉單項失敗誤停全批，或清理失敗仍繼續製造檔案。"""
    install_sink(page, **({"fail": "abort"} if failure == "cleanup" else {}))
    prepare(page, base_url)
    methods: list[str] = []

    def media(route: Route) -> None:
        """第一個項目失敗，第二個成功，據此驗證產品的後續處理。"""
        first = "item-0" in route.request.url
        method = route.request.method
        methods.append(f"{method}:{'first' if first else 'second'}")
        if first and failure == "expired":
            route.fulfill(status=410, json={"code": "token_expired"})
        elif first and failure == "http" and method == "GET":
            route.fulfill(status=502, json={"code": "upstream_media_invalid"})
        elif first and method == "GET":
            route.abort("failed")
        else:
            route.fulfill(
                status=204 if method == "HEAD" else 200, body=b"" if method == "HEAD" else b"ok"
            )

    if failure == "cleanup":
        controlled_stream(page, failure=True)
    else:
        page.route("**/api/media/**/download", media)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    if failure == "cleanup":
        expect(page.locator(".download-summary")).to_contain_text("失敗 1，未處理 1")
        assert page.evaluate("Object.keys(window.files)") == ["media-0.jpg"]
    else:
        expect(page.locator(".download-summary")).to_contain_text("已儲存 1 / 2，失敗 1")
        assert methods[-2:] == ["HEAD:second", "GET:second"]
        assert page.evaluate("window.files['media-1.jpg'].bytes") == [111, 107]
        if failure == "expired":
            expect(page.get_by_role("button", name="重新分析此項")).to_be_visible()
            assert methods == ["HEAD:first", "HEAD:second", "GET:second"]
        elif failure == "http":
            assert page.evaluate("'media-0.jpg' in window.files") is False


def test_snapshot_is_unchanged_while_picker_is_open(page: Page, base_url: str) -> None:
    """捕捉在 picker await 後才讀選取而改變下載快照。"""
    page.add_init_script(f"({PICKER_SETUP})();")
    prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-summary")).to_contain_text("選擇資料夾")
    page.locator(".media-selection").first.uncheck()
    page.evaluate(SINK_SETUP, {})
    page.evaluate("window.acceptPicker(window.testDirectory)")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 2 / 2")
    assert page.evaluate("Object.keys(window.files)") == ["media-0.jpg", "media-1.jpg"]


def test_stop_during_head_never_starts_get(page: Page, base_url: str) -> None:
    """捕捉 HEAD await 返回後未再檢查停止，導致意外 GET。"""
    install_sink(page)
    prepare(page, base_url)
    pending: list[Route] = []

    def hold_head(route: Route) -> None:
        """保持真實請求未回應，直到使用者停止。"""
        pending.append(route)

    page.route("**/api/media/**/download", hold_head)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.get_by_role("button", name="停止下載")).to_be_visible()
    page.wait_for_function(
        "document.querySelector('.download-summary').textContent.includes('資料夾模式')"
    )
    page.get_by_role("button", name="停止下載").click()
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    assert [route.request.method for route in pending] == ["HEAD"]
    assert page.evaluate("window.files") == {}


def test_in_place_reanalysis_cancels_old_writer(page: Page, base_url: str) -> None:
    """捕捉同一 group 原地替換後舊 callback 操作新結果。"""
    install_sink(page)
    prepare(page, base_url, count=1)

    def expire(route: Route) -> None:
        """先產生可操作的 recovery，不改產品內部狀態。"""
        route.fulfill(status=410, json={"code": "token_expired"})

    page.route("**/api/media/**/download", expire)
    page.get_by_role("button", name="下載選取項目").click()
    recovery = page.get_by_role("button", name="重新分析此項")
    expect(recovery).to_be_visible()
    page.unroute("**/api/media/**/download", expire)
    page.evaluate(SINK_SETUP, {"hold": "write"})
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    recovery.click()
    expect(page.locator(".selection-summary")).to_contain_text("0 / 1")
    page.evaluate("window.releaseDisk()")
    page.wait_for_function("window.files['media-0.jpg'].aborted")
    expect(page.locator(".download-summary")).to_have_text("")
    assert page.evaluate("window.files['media-0.jpg'].closed") is False


def test_batch_progress_stays_in_its_own_group(page: Page, base_url: str) -> None:
    """捕捉第一組下載時改寫第二組進度或選取狀態。"""
    install_sink(page, hold="write")
    prepare(page, base_url, count=1)
    page.get_by_role("button", name="批次分析", exact=True).click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/2")
    page.click("#analyze-batch-button")
    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.first.scroll_into_view_if_needed()
    groups.first.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    expect(groups.nth(1).locator(".selection-summary")).to_contain_text("0 / 1")
    expect(groups.nth(1).locator(".download-summary")).to_have_text("")
    groups.first.get_by_role("button", name="停止下載").click()
    page.evaluate("window.releaseDisk()")
    expect(groups.first.locator(".download-summary")).to_contain_text("已停止")


def test_stop_during_successful_close_counts_saved_file(page: Page, base_url: str) -> None:
    """重現 close 已提交卻因停止被漏計，並保證不開始下一項。"""
    install_sink(page, hold="close")
    requests = prepare(page, base_url)
    page.get_by_role("button", name="下載選取項目").click()
    expect(page.locator(".download-detail")).to_contain_text("正在完成儲存")
    page.get_by_role("button", name="停止下載").click()
    page.evaluate("window.releaseDisk()")
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    assert page.evaluate("window.files['media-0.jpg'].closed") is True
    assert page.evaluate("window.files['media-0.jpg'].bytes") == list(b"fixture-bytes")
    expect(page.locator(".download-summary")).to_contain_text("已儲存 1，失敗 0，未處理 1")
    assert requests == ["HEAD", "GET"]


def test_stop_preserves_earlier_expired_token_recovery(page: Page, base_url: str) -> None:
    """重現先過期再停止使 recovery 遺失，同時保留部分下載結果。"""
    install_sink(page, hold="write")
    prepare(page, base_url)

    def expire_first(route: Route) -> None:
        """第一項 HEAD 過期，第二項保持正常 HTTP body。"""
        if "item-0" in route.request.url:
            route.fulfill(status=410, json={"code": "token_expired"})
        else:
            route.fallback()

    page.route("**/api/media/**/download", expire_first)
    page.get_by_role("button", name="下載選取項目").click()
    page.wait_for_function("window.diskEvents.includes('write')")
    page.get_by_role("button", name="停止下載").click()
    page.evaluate("window.releaseDisk()")
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    expect(page.locator(".download-summary")).to_contain_text("已停止")
    expect(page.locator(".download-summary")).to_contain_text("失敗 1，未處理 1")
    expect(page.get_by_role("button", name="重新分析此項")).to_be_visible()


@pytest.mark.parametrize("folder", [False, True], ids=["native", "folder"])
@pytest.mark.parametrize("partial", [False, True], ids=["success", "partial-failure"])
def test_batch_summary_is_displayed_and_announced_once(
    page: Page, base_url: str, folder: bool, partial: bool
) -> None:
    """捕捉批次摘要同時寫入兩個 live status，並清除先前個別下載的舊訊息。"""
    if folder:
        install_sink(page)
    prepare(page, base_url)
    group = page.locator(".result-group")
    group.get_by_role("button", name="下載", exact=True).first.click()
    expect(group.locator(".group-status")).to_have_text("已開始下載。")

    if partial:

        def expire_first(route: Route) -> None:
            """使批次第一項過期，仍保留後續媒體正常下載。"""
            if "item-0" in route.request.url:
                route.fulfill(status=410, json={"code": "token_expired"})
            else:
                route.fallback()

        page.route("**/api/media/**/download", expire_first)

    group.get_by_role("button", name="下載選取項目").click()
    expect(group.get_by_role("button", name="下載選取項目")).to_be_enabled()
    summary = group.locator(".download-summary")
    expect(summary).to_contain_text("已儲存" if folder else "個下載已開始")
    expect(group.get_by_text(summary.inner_text(), exact=True)).to_have_count(1)
    expect(group.locator(".group-status")).to_have_text("")
    expect(summary).to_have_attribute("role", "status")
    expect(summary).to_have_attribute("aria-live", "polite")
    expect(summary).to_have_attribute("data-state", "error" if partial else "success")
