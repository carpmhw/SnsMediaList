"""以合成 API 驗證批次結果統計與不重複的生命週期公告。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_playwright_workflow as workflow

page = workflow.page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


@pytest.mark.parametrize(
    ("outcomes", "state", "summary", "result_count"),
    [
        ([True, True], "success", "成功 2、失敗 0、已停止 0", 2),
        ([False, False], "error", "成功 0、失敗 2、已停止 0", 0),
        ([True, False, True], "info", "成功 2、失敗 1、已停止 0", 2),
        ([False, True], "info", "成功 1、失敗 1、已停止 0", 1),
    ],
)
def test_final_status_reflects_actual_batch_outcomes(
    page: Page,
    base_url: str,
    outcomes: list[bool],
    state: str,
    summary: str,
    result_count: int,
) -> None:
    """全部／部分失敗不能冒充成功，完成數納入失敗並保留成功結果。"""
    calls: list[str] = []

    def respond(route: Route) -> None:
        """依固定結果矩陣回應，原始假敏感訊息不得顯示。"""
        index = len(calls)
        source = route.request.post_data_json["url"]
        calls.append(source)
        if outcomes[index]:
            route.fulfill(json={**workflow.SUCCESS_PAYLOAD, "post_url": source})
        else:
            route.fulfill(
                status=504,
                json={"code": "extraction_timeout", "message": "raw-sensitive-fixture"},
            )

    page.route("**/api/extractions", respond)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    urls = [f"https://x.com/creator/status/{number}" for number in range(1, len(outcomes) + 1)]
    page.locator("#batch-post-urls").fill("\n".join(urls))
    page.locator("#analyze-batch-button").click()
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    status = page.locator("#status")
    expect(status).to_have_attribute("data-state", state)
    expect(status).to_contain_text(summary)
    expect(page.locator("#queue-progress")).to_contain_text(
        f"已處理 {len(outcomes)} / {len(outcomes)}"
    )
    expect(page.locator(".result-group")).to_have_count(result_count)
    expect(page.locator("body")).not_to_contain_text("raw-sensitive-fixture")
    if result_count == 0:
        expect(status).not_to_contain_text("準備就緒")
        expect(status).not_to_contain_text("下載")
    assert calls == urls


@pytest.mark.parametrize("success", [True, False])
def test_stop_summary_waits_for_running_request(page: Page, base_url: str, success: bool) -> None:
    """停止只略過未開始項目，執行中請求完成後才公告實際結果。"""
    held: list[Route] = []

    def hold(route: Route) -> None:
        """暫留執行中項目，不需要真實平台或 Cookie。"""
        held.append(route)

    page.route("**/api/extractions", hold)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "https://x.com/creator/status/1\n"
        "https://x.com/creator/status/2\n"
        "https://x.com/creator/status/3"
    )
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".queue-item[data-state='running']")).to_have_count(1)
    page.locator("#stop-batch-button").click()
    expect(page.locator("#status")).to_contain_text("等待第 1 項完成")
    expect(page.locator("#analyze-batch-button")).to_be_disabled()
    expect(page.locator(".queue-item[data-state='stopped']")).to_have_count(2)
    assert len(held) == 1
    if success:
        held[0].fulfill(json=workflow.SUCCESS_PAYLOAD)
    else:
        held[0].fulfill(status=504, json={"code": "extraction_timeout"})
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    status = page.locator("#status")
    expect(status).to_contain_text("批次已停止")
    expect(status).to_have_attribute("data-state", "info")
    expect(status).to_contain_text(
        "成功 1、失敗 0、已停止 2" if success else "成功 0、失敗 1、已停止 2"
    )
    expect(page.locator("#queue-progress")).to_contain_text("已處理 1 / 3")
    assert len(held) == 1


def test_batch_lifecycle_uses_status_outside_busy_workbench(page: Page, base_url: str) -> None:
    """批次更新由工作台外公告，清單與骨架不再各自作重複 live 更新。"""
    held: list[Route] = []

    def hold(route: Route) -> None:
        """保持請求未完成以驗證真正的 busy 與進度呈現。"""
        held.append(route)

    page.route("**/api/extractions", hold)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill("https://x.com/creator/status/1")
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".analysis-workbench")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#status")).to_contain_text("正在分析第 1 項")
    expect(page.locator("#status")).to_have_attribute("aria-live", "polite")
    assert page.locator("#status").evaluate(
        "/** 公告不能被祖先 busy 狀態延後。 */ el => el.closest('[aria-busy=\"true\"]') === null"
    )
    expect(
        page.locator("#queue-status [aria-live='polite'], #queue-status [role='status']")
    ).to_have_count(0)
    expect(page.locator("#analysis-loading")).to_have_attribute("aria-live", "off")
    expect(page.locator(".queue-item")).to_contain_text("執行中")
    page.locator("#single-mode-button").click()
    expect(page.locator("#queue-status")).to_be_hidden()
    held[0].fulfill(status=504, json={"code": "extraction_timeout"})
    expect(page.locator("#status")).to_have_attribute("data-state", "error")
    expect(page.locator("#status")).to_contain_text("失敗 1")
    expect(page.locator("#status")).to_be_visible()


def test_single_loading_stays_announced_after_batch(page: Page, base_url: str) -> None:
    """批次停用骨架公告不影響後續單筆分析，且新批次不沿用舊統計。"""
    calls: list[Route] = []

    def hold(route: Route) -> None:
        """逐次保存請求，由測試明確完成每一輪分析。"""
        calls.append(route)

    page.route("**/api/extractions", hold)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill("https://x.com/creator/status/1")
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".queue-item[data-state='running']")).to_have_count(1)
    calls[0].fulfill(status=504, json={"code": "extraction_timeout"})
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".queue-item[data-state='running']")).to_have_count(1)
    expect(page.locator("#queue-progress")).to_contain_text("已處理 0 / 1")
    calls[1].fulfill(json=workflow.SUCCESS_PAYLOAD)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/2")
    page.locator("#analyze-button").click()
    expect(page.locator("#analysis-loading")).to_have_attribute("aria-live", "polite")
    expect(page.locator("#analysis-loading")).to_be_visible()
    calls[2].fulfill(json=workflow.SUCCESS_PAYLOAD)
    expect(page.locator("#analysis-loading")).to_be_hidden()
    expect(page.locator("#status")).to_have_attribute("data-state", "success")


def test_stop_completion_does_not_repeat_identical_announcement(page: Page, base_url: str) -> None:
    """停止完成時即使重繪清單，也只對 live region 寫入一次相同終態。"""
    held: list[Route] = []

    def hold(route: Route) -> None:
        """暫留第一筆以觀察停止後兩次 queue render 的公告副作用。"""
        held.append(route)

    page.route("**/api/extractions", hold)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2"
    )
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".queue-item[data-state='running']")).to_have_count(1)
    page.locator("#stop-batch-button").click()
    page.evaluate(
        """/** 觀察真正 live region 的新增訊息節點，不攔截應用程式方法。 */ () => {
          window.batchAnnouncements = [];
          new MutationObserver(
            /** 收集實際加入的訊息，而不是只讀最後一個 DOM 狀態。 */
            records => {
              for (const record of records) {
                for (const node of record.addedNodes) {
                  window.batchAnnouncements.push(node.textContent);
                }
              }
            }).observe(document.querySelector('#status'), { childList: true });
        }"""
    )
    held[0].fulfill(json=workflow.SUCCESS_PAYLOAD)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    messages: list[str] = page.evaluate(
        "/** 讀取已發生的公告更新，不額外觸發重繪。 */ () => window.batchAnnouncements"
    )
    assert len(messages) == 1
    assert "批次已停止" in messages[0]
