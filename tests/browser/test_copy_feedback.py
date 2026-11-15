"""驗證卡片複製檔名的可存取回饋與獨立清理期限。"""

import pytest
from playwright.sync_api import Page, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "denied", "missing"])
def test_copy_outcome_has_separate_visible_status(
    engine_page: Page, base_url: str, outcome: str
) -> None:
    """只複製檔名，兩種失敗與成功皆不改按鈕名稱、焦點或下載能力。"""
    page = engine_page
    page.add_init_script(
        """/** 只替換瀏覽器剪貼簿邊界，記錄產品真正送出的文字。 */ {
          window.copiedNames = [];
          Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
            /** 保留實際寫入值供測試比對。 */
            writeText: async text => { window.copiedNames.push(text); }
          }});
        }"""
    )
    viewer_workflow.open_results(page, base_url)
    card = page.locator(".media-card").first
    button = card.locator(".copy-filename")
    feedback = card.locator(".copy-feedback")
    expect(feedback).to_have_attribute("role", "status")
    expect(feedback).to_have_text("")
    if outcome == "missing":
        page.evaluate(
            "/** 模擬非安全來源沒有Clipboard介面。 */ () => "
            "Object.defineProperty(navigator, 'clipboard', {value: undefined})"
        )
    elif outcome == "denied":
        page.evaluate(
            """/** 權限拒絕只影響外部API，不替換產品邏輯。 */ () => {
              navigator.clipboard.writeText = async () => { throw new Error('PRIVATE_SENTINEL'); };
            }"""
        )
    button.focus()
    button.press("Enter")
    expected = "已複製檔名" if outcome == "success" else "無法複製檔名"
    expect(feedback).to_have_text(expected)
    expect(feedback).to_be_visible()
    expect(button).to_have_text("複製檔名")
    expect(button).to_be_enabled()
    expect(button).to_be_focused()
    expect(card.locator(".download-action")).to_be_enabled()
    expect(page.locator("#media-viewer")).to_be_hidden()
    assert page.evaluate("/** 讀取送出的檔名。 */ () => window.copiedNames") == (
        ["image-1.png"] if outcome == "success" else []
    )
    expect(card).not_to_contain_text("PRIVATE_SENTINEL")
    expect(feedback).to_have_text("", timeout=4000)


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_repeated_copy_restarts_only_its_own_feedback_timer(
    engine_page: Page, base_url: str
) -> None:
    """真實產品計時器搭配虛擬時鐘，舊期限不清除新訊息且多卡片互不覆蓋。"""
    page = engine_page
    page.add_init_script(
        """/** 剪貼簿邊界成功，不模擬產品回饋或計時器。 */ {
          Object.defineProperty(navigator, 'clipboard', { value: {
            /** 模擬成功寫入。 */ writeText: async () => {}
          }});
        }"""
    )
    viewer_workflow.open_results(page, base_url)
    page.clock.install()
    first = page.locator(".media-card").nth(0)
    second = page.locator(".media-card").nth(1)
    first.locator(".copy-filename").click()
    expect(first.locator(".copy-feedback")).to_have_text("已複製檔名")
    original = first.locator(".copy-feedback").text_content()
    page.clock.run_for(1000)
    first.locator(".copy-filename").click()
    expect(first.locator(".copy-feedback")).to_have_text("已複製檔名")
    assert first.locator(".copy-feedback").text_content() != original
    page.clock.run_for(500)
    second.locator(".copy-filename").click()
    expect(second.locator(".copy-feedback")).to_have_text("已複製檔名")
    page.clock.run_for(600)
    expect(first.locator(".copy-feedback")).to_have_text("已複製檔名")
    expect(second.locator(".copy-feedback")).to_have_text("已複製檔名")
    page.clock.run_for(950)
    expect(first.locator(".copy-feedback")).to_have_text("")
    expect(second.locator(".copy-feedback")).to_have_text("已複製檔名")
    page.clock.run_for(500)
    expect(second.locator(".copy-feedback")).to_have_text("")
