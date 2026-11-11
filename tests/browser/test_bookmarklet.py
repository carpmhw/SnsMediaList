"""以本機瀏覽器驗證書籤入口、來源預填及新分頁隔離。"""

import json
from urllib.parse import quote, urlsplit

import pytest
from playwright.sync_api import Page, Request, Route, expect

from sns_media_list.errors import AppError
from sns_media_list.url_validation import validate_post_url
from tests.browser import test_playwright_workflow as workflow

page = workflow.page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.fixture
def api_requests(page: Page) -> list[Request]:
    """攔截分析與媒體邊界，記錄實際請求而不連線外部平台。"""
    requests: list[Request] = []

    def respond(route: Route) -> None:
        """僅分析端點提供既有合成 payload，其他 API 不提供媒體。"""
        requests.append(route.request)
        if route.request.url.endswith("/api/extractions"):
            route.fulfill(
                content_type="application/json", body=json.dumps(workflow.SUCCESS_PAYLOAD)
            )
        else:
            route.fulfill(status=404)

    page.route("**/api/**", respond)
    return requests


def consume_fragment(page: Page, fragment: str) -> dict[str, object]:
    """在已載入頁面直接消費入口，以測試真實 history 副作用。"""
    return page.evaluate(
        """/** 設定一次入口並回傳消費結果與網址。 */
        async (fragment) => {
          const { consumeSourceFragment } = await import('/bookmarklet.js');
          history.replaceState({ marker: 'kept' }, '', '/?keep=1' + fragment);
          const result = consumeSourceFragment(location, history);
          return { result, hash: location.hash, search: location.search, state: history.state };
        }""",
        fragment,
    )


def test_source_fragment_round_trip(page: Page, base_url: str) -> None:
    """來源特殊字元不被當參數，且解碼與 URL 解析前已清除 fragment。"""
    page.goto(base_url)
    source = "https://x.com/creator/status/1?text=中文&literal=a+b%25&quote='"
    outcome = page.evaluate(
        """/** 觀察解析邊界執行時的真實網址，不取代 history。 */
        async (source) => {
          const { consumeSourceFragment } = await import('/bookmarklet.js');
          history.replaceState({ marker: 'kept' }, '',
            '/?keep=1#source=' + encodeURIComponent(source));
          const originalDecode = window.decodeURIComponent;
          const OriginalURL = window.URL;
          const observations = [];
          /** 記錄解碼時來源是否已移除。 */
          window.decodeURIComponent = (value) => {
            observations.push(location.hash === '');
            return originalDecode(value);
          };
          /** 記錄建立 URL 時來源是否已移除。 */
          window.URL = class extends OriginalURL {
            /** 沿用真正 URL parser 並觀察呼叫順序。 */
            constructor(...args) {
              observations.push(location.hash === '');
              super(...args);
            }
          };
          try {
            return {
              result: consumeSourceFragment(location, history), observations,
              hash: location.hash, search: location.search, state: history.state,
            };
          } finally {
            window.decodeURIComponent = originalDecode;
            window.URL = OriginalURL;
          }
        }""",
        source,
    )
    assert outcome["result"] == {"kind": "valid", "source": source}
    assert outcome["observations"] and all(outcome["observations"])
    assert outcome["hash"] == ""
    assert outcome["search"] == "?keep=1"
    assert outcome["state"] == {"marker": "kept"}


@pytest.mark.parametrize(
    "fragment",
    [
        "#source=",
        "#source",
        "#source=%",
        "#source=%E0%A4%A",
        "#source=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1&source=ignored",
        "#source=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1&next=ignored",
        "#next=ignored&source=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1",
    ],
)
def test_source_fragment_invalid(page: Page, base_url: str, fragment: str) -> None:
    """畸形、重複及混合參數整次拒絕，但仍立即清除。"""
    page.goto(base_url)
    outcome = consume_fragment(page, fragment)
    assert outcome["result"] == {"kind": "invalid"}
    assert outcome["hash"] == ""


def test_source_fragment_length_limit(page: Page, base_url: str) -> None:
    """2048 字元可帶入，2049 與空白輸入不截斷、不預填。"""
    page.goto(base_url)
    prefix = "https://x.com/creator/status/1?q="
    source = prefix + "a" * (2048 - len(prefix))
    assert consume_fragment(page, "#source=" + quote(source, safe=""))["result"] == {
        "kind": "valid",
        "source": source,
    }
    for invalid in [
        source + "a",
        " ",
        "https://x.com/creator/status/1\n",
        "https:\\\\x.com/a/status/1",
    ]:
        outcome = consume_fragment(page, "#source=" + quote(invalid, safe=""))
        assert outcome["result"] == {"kind": "invalid"}
        assert outcome["hash"] == ""


@pytest.mark.parametrize("fragment", ["", "#results", "#other=value"])
def test_unrelated_fragment_untouched(page: Page, base_url: str, fragment: str) -> None:
    """無入口的普通錨點不被清除，既有 query 與 state 亦保留。"""
    page.goto(base_url)
    outcome = consume_fragment(page, fragment)
    assert outcome == {
        "result": {"kind": "none"},
        "hash": fragment,
        "search": "?keep=1",
        "state": {"marker": "kept"},
    }


def test_source_url_policy(page: Page, base_url: str) -> None:
    """一般來源接受規則與後端一致，不擅自新增平台或路徑。"""
    page.goto(base_url)
    accepted = [
        "https://instagram.com/p/abc/",
        "https://www.instagram.com/reel/abc/",
        "https://instagram.com/tv/abc/",
        "https://instagram.com/creator/p/abc/",
        "https://instagram.com/creator/reel/abc/",
        "https://instagram.com/creator/tv/abc/",
        "https://instagram.com/stories/creator/",
        "https://instagram.com/stories/a.b_1/123/",
        "https://instagram.com/stories/me/123/",
        "https://x.com/creator/status/123",
        "https://www.x.com/creator/status/123/",
        "https://twitter.com/creator/status/123",
        "https://www.twitter.com/i/web/123",
        "https://X.COM.:443/creator/status/123?text=a+b",
    ]
    rejected = [
        "http://x.com/creator/status/1",
        "https://user:pass@x.com/creator/status/1",
        "https://x.com:444/creator/status/1",
        "https://x.com.attacker.invalid/creator/status/1",
        "https://x.com/creator/status/1#",
        "https://x.com/creator/status/no-id",
        "https://x.com/i/web/status/1",
        "https://x.com/creator",
        "https://instagram.com/creator/",
        "https://instagram.com/stories/",
        "https://instagram.com/stories/highlights/123/",
        "https://instagram.com/stories/me/",
        "https://instagram.com/stories/.creator/",
        "https://instagram.com/stories/creator./",
        "https://instagram.com/stories/a..b/",
        "https://instagram.com/stories/creator",
        "https://instagram.com/stories/creator/non-numeric/",
        "https://instagram.com/stories/" + "a" * 31 + "/",
        "javascript:alert(1)",
        "not a URL",
    ]
    for source in accepted:
        validate_post_url(source)
        outcome = consume_fragment(page, "#source=" + quote(source, safe=""))
        assert outcome["result"] == {"kind": "valid", "source": source}, source
    for source in rejected:
        with pytest.raises(AppError):
            validate_post_url(source)
        outcome = consume_fragment(page, "#source=" + quote(source, safe=""))
        assert outcome["result"] == {"kind": "invalid"}, source


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_bookmarklet_installation(page: Page, base_url: str, width: int) -> None:
    """安裝區預設收合、可鍵盤展開，窄視窗亦能選取完整安裝碼。"""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    tools = page.locator("#bookmarklet-tools")
    expect(tools).to_have_count(1)
    expect(tools).not_to_have_attribute("open", "")
    summary = tools.locator("summary")
    summary.focus()
    summary.press("Enter")
    expect(tools).to_have_attribute("open", "")
    link = page.get_by_role("link", name="送到 SNS Media List")
    href = link.get_attribute("href")
    assert href and href.startswith("javascript:")
    code = page.get_by_label("手動安裝碼")
    expect(code).to_have_value(href)
    expect(code).to_have_attribute("readonly", "")
    expect(tools).to_contain_text("拖到書籤列")
    expect(tools).to_contain_text("不要直接點")
    code.focus()
    code.press("ControlOrMeta+A")
    selected_length = code.evaluate(
        "/** 確認唯讀安裝碼仍可完整選取。 */ el => el.selectionEnd - el.selectionStart"
    )
    assert selected_length == len(href)
    assert page.evaluate(
        "/** 檢查整頁未水平溢出。 */ () => document.documentElement.scrollWidth <= innerWidth"
    )


def test_source_prefill_requires_submit(
    page: Page, base_url: str, api_requests: list[Request]
) -> None:
    """來源只預填及聚焦，使用者提交後才建立一個分析請求。"""
    source = "https://x.com/creator/status/1?literal=a+b%25&text=中文"
    page.goto(base_url + "/#source=" + quote(source, safe=""))
    field = page.get_by_label("內容 URL", exact=True)
    expect(field).to_have_value(source)
    expect(field).to_be_focused()
    expect(page.locator("#single-mode-button")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#status")).to_have_text("已帶入來源網址，請確認後按「分析」。")
    assert page.url == base_url + "/"
    assert api_requests == []
    page.get_by_role("button", name="分析", exact=True).click()
    expect(page.locator("#results")).to_be_visible()
    assert len(api_requests) == 1
    assert api_requests[0].method == "POST"
    assert api_requests[0].post_data_json == {"url": source}


def test_invalid_prefill_stays_manual(
    page: Page, base_url: str, api_requests: list[Request]
) -> None:
    """非法來源不預填或分析，但使用者仍可手動貼上並提交。"""
    page.goto(base_url + "/#source=%E0%A4%A")
    field = page.get_by_label("內容 URL", exact=True)
    expect(page.locator("#status")).to_have_text(
        "無法帶入來源網址，請手動貼上支援的 Instagram 或 X 內容 URL。"
    )
    expect(field).to_have_value("")
    assert page.url == base_url + "/"
    assert api_requests == []
    field.fill("https://x.com/creator/status/1")
    page.get_by_role("button", name="分析", exact=True).click()
    expect(page.locator("#results")).to_be_visible()
    assert len(api_requests) == 1


def test_prefill_is_once_only(page: Page, base_url: str, api_requests: list[Request]) -> None:
    """重新整理與載入後 hashchange 不重播來源，且不儲存來源歷史。"""
    source = "https://x.com/creator/status/1"
    page.goto(base_url + "/#source=" + quote(source, safe=""))
    field = page.get_by_label("內容 URL", exact=True)
    expect(field).to_have_value(source)
    storage = page.evaluate(
        "/** 檢查未寫入來源儲存。 */ () => [localStorage.length, sessionStorage.length]"
    )
    assert storage == [0, 0]
    page.reload()
    expect(field).to_be_focused()
    expect(field).to_have_value("")
    page.evaluate(
        """/** 模擬載入後修改錨點。 */ source => {
          location.hash = "source=" + encodeURIComponent(source);
        }""",
        source,
    )
    page.evaluate(
        """/** 等待 hashchange 所屬事件回合完成。 */
        () => new Promise(
          /** 在下一個畫面確認事件已派發。 */ resolve => requestAnimationFrame(resolve))"""
    )
    expect(field).to_have_value("")
    page.goto(base_url + "/?fresh=1#results")
    expect(page.locator("#bookmarklet-code")).not_to_have_value("")
    expect(field).to_have_value("")
    assert page.url == base_url + "/?fresh=1#results"
    assert api_requests == []


def test_fixed_entry_ignores_page_parameters(page: Page, base_url: str) -> None:
    """產生的書籤只固定當下服務位置，不夾帶 query、來源或可覆寫目的地。"""
    source = "https://x.com/creator/status/1?next=https://example.invalid"
    for entry in [base_url, base_url.replace("127.0.0.1", "localhost")]:
        page.goto(entry + "/?next=https://example.invalid#source=" + quote(source, safe=""))
        expect(page.locator("#post-url")).to_have_value(source)
        code = page.locator("#bookmarklet-link").get_attribute("href")
        assert code is not None
        destination = page.evaluate(
            """/** 執行實際生成碼，僅此單測攔截目的站參數。 */
            code => {
              const original = window.open;
              let target;
              /** 捕捉書籤所指定的唯一目的站。 */
              window.open = url => { target = url; };
              try { eval(decodeURIComponent(code.slice('javascript:'.length))); }
              finally { window.open = original; }
              return target;
            }""",
            code,
        )
        assert destination.startswith(entry + "/#source=")
        assert "?next=" not in destination.split("#")[0]


def test_bookmarklet_opens_isolated_tab(page: Page, base_url: str) -> None:
    """實際執行生成的 javascript URL，不以 window.open 替身冒充隔離證據。"""
    page.goto(base_url)
    expect(page.locator("#bookmarklet-code")).not_to_have_value("")
    code = page.locator("#bookmarklet-link").get_attribute("href")
    assert code is not None
    source = (
        "https://x.com/creator/status/1?text=a%26b%2Bc%25&quote=%22&next=https://example.invalid"
    )
    documents: list[Request] = []
    api: list[Request] = []

    def isolate_network(route: Route) -> None:
        """來源頁僅用合成 HTML，所有非本機目的地均不連外。"""
        request = route.request
        if request.url == source:
            route.fulfill(content_type="text/html", body='<a id="send">送到服務</a>')
        elif request.url.startswith(base_url + "/"):
            if urlsplit(request.url).path.startswith("/api/"):
                api.append(request)
                route.abort()
            else:
                if request.is_navigation_request():
                    documents.append(request)
                route.continue_()
        else:
            route.abort()

    page.context.route("**/*", isolate_network)
    page.goto(source)
    page.locator("#send").evaluate(
        "/** 安裝實際生成的書籤碼供點擊。 */ (el, code) => { el.href = code; }", code
    )
    with page.context.expect_event("page") as opened:
        page.locator("#send").click()
    target = opened.value
    try:
        expect(target.locator("#post-url")).to_have_value(source)
        expect(target.locator("#post-url")).to_be_focused()
        assert target.url == base_url + "/"
        assert target.evaluate("/** 檢查新頁無 opener。 */ () => window.opener === null")
        assert target.evaluate("/** 檢查新頁不含來源 Referer。 */ () => document.referrer") == ""
        assert len(documents) == 1
        assert documents[0].url == base_url + "/"
        assert "referer" not in documents[0].all_headers()
        assert api == []
        assert page.url == source
    finally:
        target.close()


def test_bookmarklet_special_entry_serialization(page: Page, base_url: str) -> None:
    """原生 javascript URL 解碼不會將服務路徑中的百分比或引號變成程式。"""
    page.goto(base_url)
    entry = base_url + "/entry%22%25%27/"
    code = page.evaluate(
        """/** 以含編碼引號的可信入口產生實際書籤。 */ async entry => {
          const { buildBookmarklet } = await import('/bookmarklet.js');
          return buildBookmarklet(entry);
        }""",
        entry,
    )

    def destination(route: Route) -> None:
        """只提供測試入口，避免將特殊路徑誤當真實部署支援。"""
        route.fulfill(content_type="text/html", body="接收入口")

    page.context.route(entry + "**", destination)
    page.evaluate(
        """/** 建立供真實點擊的書籤連結。 */ code => {
          const link = document.createElement('a');
          link.id = 'special-entry'; link.textContent = '測試入口'; link.href = code;
          document.body.append(link);
        }""",
        code,
    )
    with page.context.expect_event("page") as opened:
        page.locator("#special-entry").click()
    target = opened.value
    try:
        target.wait_for_load_state()
        assert target.url == entry + "#source=" + quote(base_url + "/", safe="~()*!.'-")
        assert target.evaluate("/** 確認特殊入口仍無 opener。 */ () => opener === null")
    finally:
        target.close()
