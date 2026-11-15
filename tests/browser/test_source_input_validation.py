"""驗證來源欄位錯誤、原生驗證與使用者主動操作的焦點行為。"""

from urllib.parse import quote

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_playwright_workflow as workflow

page = workflow.page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.fixture
def extraction_calls(page: Page) -> list[str]:
    """以合成成功回應記錄實際分析請求，避免連外。"""
    calls: list[str] = []

    def respond(route: Route) -> None:
        """記錄提交來源並回傳完整的既有成功 fixture。"""
        calls.append(route.request.post_data_json["url"])
        route.fulfill(json=workflow.SUCCESS_PAYLOAD)

    page.route("**/api/extractions", respond)
    return calls


def test_initial_input_is_neutral_without_focus(
    page: Page, base_url: str, extraction_calls: list[str]
) -> None:
    """移除無條件聚焦，避免一進頁面就跳過文案或進入輸入狀態。"""
    page.goto(base_url)
    expect(page.locator("#post-url")).not_to_be_focused()
    for selector in ["#post-url", "#batch-post-urls"]:
        expect(page.locator(selector)).to_have_attribute("aria-invalid", "false")
    expect(page.locator("#post-url-error")).to_be_hidden()
    expect(page.locator("#batch-post-urls-error")).to_be_hidden()
    assert extraction_calls == []


@pytest.mark.parametrize("value", ["", "not-a-url"])
def test_native_rejection_links_inline_error_and_recovers(
    page: Page, base_url: str, extraction_calls: list[str], value: str
) -> None:
    """原生拒絕仍聚焦欄位，提示與欄位關聯，修正後可再次提交。"""
    page.goto(base_url)
    field = page.locator("#post-url")
    field.fill(value)
    page.locator("#analyze-button").click()
    expect(field).to_be_focused()
    expect(field).to_have_attribute("aria-invalid", "true")
    error = page.locator("#post-url-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("請輸入")
    assert "post-url-error" in (field.get_attribute("aria-describedby") or "").split()
    assert extraction_calls == []
    field.fill("https://x.com/creator/status/1")
    expect(field).to_have_attribute("aria-invalid", "false")
    expect(error).to_be_hidden()
    assert "post-url-error" not in (field.get_attribute("aria-describedby") or "").split()
    assert "url-help" in (field.get_attribute("aria-describedby") or "").split()
    page.locator("#analyze-button").click()
    expect(page.locator("#results")).to_be_visible()
    assert extraction_calls == ["https://x.com/creator/status/1"]


def test_first_typing_does_not_announce_error(page: Page, base_url: str) -> None:
    """使用者第一次尚在輸入不完整網址時，不提前報錯。"""
    page.goto(base_url)
    field = page.locator("#post-url")
    field.fill("https:/")
    expect(field).to_have_attribute("aria-invalid", "false")
    expect(page.locator("#post-url-error")).to_be_hidden()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (" \n ", "請至少輸入 1 個 URL。"),
        (
            "\n".join(f"https://x.com/creator/status/{number}" for number in range(1, 7)),
            "最多只能分析 5 個 URL。",
        ),
    ],
)
def test_batch_rejection_focuses_field_and_clears_after_correction(
    page: Page, base_url: str, extraction_calls: list[str], value: str, message: str
) -> None:
    """整批拒絕有欄位錯誤與焦點，修正不觸發請求，明確提交後才分析。"""
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    field = page.locator("#batch-post-urls")
    field.fill(value)
    page.locator("#analyze-batch-button").click()
    expect(field).to_be_focused()
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#batch-post-urls-error")).to_have_text(message)
    assert "batch-post-urls-error" in (field.get_attribute("aria-describedby") or "").split()
    assert extraction_calls == []
    field.fill("https://x.com/creator/status/1\nhttps://x.com/creator/status/1\n\n")
    expect(field).to_have_attribute("aria-invalid", "false")
    expect(page.locator("#batch-post-urls-error")).to_be_hidden()
    assert "url-help" in (field.get_attribute("aria-describedby") or "").split()
    assert "batch-post-urls-error" not in (field.get_attribute("aria-describedby") or "").split()
    assert extraction_calls == []
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".media-card")).to_have_count(1)
    assert extraction_calls == ["https://x.com/creator/status/1"]


def test_mode_switch_excludes_inactive_constraint_and_keeps_newlines(
    page: Page, base_url: str, extraction_calls: list[str]
) -> None:
    """隱藏欄位不參與 constraints，模式切換保留文字且批次 Enter 仍為換行。"""
    page.goto(base_url)
    single = page.locator("#post-url")
    single.fill("not-a-url")
    page.locator("#batch-mode-button").click()
    expect(single).to_be_disabled()
    batch = page.locator("#batch-post-urls")
    expect(batch).to_be_focused()
    batch.fill("https://x.com/creator/status/1")
    batch.press("End")
    batch.press("Enter")
    expect(batch).to_have_value("https://x.com/creator/status/1\n")
    assert page.locator("#extraction-form").evaluate(
        "/** 隱藏單筆欄位不讓批次表單成為 invalid。 */ form => form.checkValidity()"
    )
    assert extraction_calls == []
    page.locator("#single-mode-button").click()
    expect(single).to_be_enabled()
    expect(single).to_be_focused()
    expect(single).to_have_value("not-a-url")
    expect(batch).to_be_disabled()
    page.locator("#batch-mode-button").click()
    expect(batch).to_have_value("https://x.com/creator/status/1\n")


@pytest.mark.parametrize(
    ("code", "field_error"),
    [("invalid_url", True), ("unsupported_url", True), ("extraction_timeout", False)],
)
def test_server_errors_only_mark_relevant_input(
    page: Page, base_url: str, code: str, field_error: bool
) -> None:
    """只有網址相關 API 拒絕標記欄位，不將平台或服務失敗誤當輸入錯誤。"""

    def reject(route: Route) -> None:
        """原始訊息含假敏感值，前端仍只能使用固定的安全映射。"""
        route.fulfill(status=400, json={"code": code, "message": "raw-sensitive-fixture"})

    page.route("**/api/extractions", reject)
    page.goto(base_url)
    field = page.locator("#post-url")
    field.fill("https://x.com/creator")
    page.locator("#analyze-button").click()
    expect(page.locator("#status")).to_have_attribute("data-state", "error")
    expect(page.locator("body")).not_to_contain_text("raw-sensitive-fixture")
    expect(field).to_have_attribute("aria-invalid", "true" if field_error else "false")
    if field_error:
        expect(field).to_be_focused()
        expect(page.locator("#post-url-error")).to_be_visible()
        field.fill("https://x.com/creator/status/1")
        expect(field).to_have_attribute("aria-invalid", "false")
        expect(page.locator("#post-url-error")).to_be_hidden()


@pytest.mark.parametrize("change_mode", [False, True])
def test_pending_rejection_does_not_focus_a_changed_or_hidden_source(
    page: Page, base_url: str, change_mode: bool
) -> None:
    """進行中修改來源或切到批次後，舊網址拒絕不應搶焦點或標記新輸入。"""
    held: list[Route] = []

    def hold(route: Route) -> None:
        """暫留分析回應以模擬使用者在等待期間修改欄位。"""
        held.append(route)

    page.route("**/api/extractions", hold)
    page.goto(base_url)
    field = page.locator("#post-url")
    field.fill("https://x.com/creator")
    page.locator("#analyze-button").click()
    expect(page.locator("#analyze-button")).to_be_disabled()
    field.press("Enter")
    if change_mode:
        page.locator("#batch-mode-button").click()
        target = page.locator("#batch-post-urls")
    else:
        field.fill("https://x.com/creator/status/1")
        target = field
    expect(target).to_be_focused()
    assert len(held) == 1
    held[0].fulfill(status=400, json={"code": "unsupported_url"})
    expect(page.locator("#status")).to_have_attribute("data-state", "error")
    expect(field).to_have_attribute("aria-invalid", "false")
    expect(page.locator("#post-url-error")).to_be_hidden()
    expect(target).to_be_focused()


def test_bookmarklet_prefill_does_not_focus_or_submit(
    page: Page, base_url: str, extraction_calls: list[str]
) -> None:
    """書籤帶入仍要求確認，不因預填自動聚焦或提交。"""
    source = "https://x.com/creator/status/1"
    page.goto(base_url + "/#source=" + quote(source, safe=""))
    expect(page.locator("#post-url")).to_have_value(source)
    expect(page.locator("#post-url")).not_to_be_focused()
    expect(page.locator("#status")).to_contain_text("請確認後按")
    assert extraction_calls == []
    page.locator("#analyze-button").click()
    expect(page.locator("#results")).to_be_visible()
    assert extraction_calls == [source]


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_inline_errors_fit_each_viewport(page: Page, base_url: str, width: int) -> None:
    """實際欄位提示在手機到桌面尺寸均可見，且不產生整頁水平溢出。"""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    page.locator("#analyze-button").click()
    expect(page.locator("#post-url-error")).to_be_visible()
    page.locator("#batch-mode-button").click()
    page.locator("#analyze-batch-button").click()
    expect(page.locator("#batch-post-urls-error")).to_be_visible()
    assert page.evaluate(
        "/** 檢查欄位錯誤沒有造成水平溢出。 */ "
        "() => document.documentElement.scrollWidth <= innerWidth"
    )
