"""以匹配的 Chromium／Firefox 驗證群組導覽可見性與動態偏好。"""

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser import test_media_viewer as viewer_workflow
from tests.browser import test_playwright_workflow as workflow

engine_page = viewer_workflow.engine_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


def open_groups(page: Page, base_url: str, count: int) -> list[str]:
    """以真實前端批次流程建立指定群組，回傳實際提交的來源紀錄。"""
    calls: list[str] = []

    def respond(route: Route) -> None:
        """每個合成來源使用自己的 canonical URL，不連線平台。"""
        source = route.request.post_data_json["url"]
        calls.append(source)
        route.fulfill(json={**workflow.SUCCESS_PAYLOAD, "post_url": source})

    page.route("**/api/extractions", respond)
    page.goto(base_url)
    page.locator("#batch-mode-button").click()
    page.locator("#batch-post-urls").fill(
        "\n".join(f"https://x.com/creator/status/{index}" for index in range(1, count + 1))
    )
    page.locator("#analyze-batch-button").click()
    expect(page.locator(".result-group")).to_have_count(count)
    expect(page.locator("#analyze-batch-button")).to_be_enabled()
    return calls


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_navigation_obeys_motion_preference(
    engine_page: Page, base_url: str, width: int, motion: str
) -> None:
    """同一事件回合檢查 reduced motion 已到目的地，避免等待掩蓋強制動畫。"""
    page = engine_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion=motion)
    calls = open_groups(page, base_url, 2)
    track = page.locator("#result-groups")
    expect(track).to_have_css("scroll-behavior", "auto" if motion == "reduce" else "smooth")
    movement = page.locator("#next-result-group").evaluate(
        """/** 同回合啟動並取樣，避免兩次工具呼叫之間動畫已完成而漏掉中間幀。 */
        async button => {
          const track = document.querySelector('#result-groups');
          const target = track.querySelectorAll('.result-group')[1];
          const destination = target.offsetLeft - track.querySelector('.result-group').offsetLeft;
          button.click();
          const residual = Math.abs(
            target.getBoundingClientRect().left - track.getBoundingClientRect().left);
          const positions = [track.scrollLeft];
          if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
            const deadline = performance.now() + 3000;
            while (performance.now() < deadline) {
              await new Promise(
                /** 每幀只觀察真實scrollLeft，不替換捲動API。 */ resolve =>
                  requestAnimationFrame(resolve));
              positions.push(track.scrollLeft);
              if (Math.abs(target.getBoundingClientRect().left
                - track.getBoundingClientRect().left) < 1) break;
            }
          }
          return { residual, positions, destination };
        }"""
    )
    if motion == "reduce":
        assert movement["residual"] < 1
    else:
        assert movement["residual"] > 1
        positions = movement["positions"]
        destination = movement["destination"]
        assert any(1 < position < destination - 1 for position in positions), positions
        assert abs(positions[-1] - destination) < 1, positions
    page.wait_for_function(
        """/** 一般偏好可等待動畫，但最終必須真正到達第二組。 */ () => {
          const track = document.querySelector('#result-groups');
          const target = track.querySelectorAll('.result-group')[1];
          return Math.abs(
            target.getBoundingClientRect().left - track.getBoundingClientRect().left) < 1;
        }"""
    )
    expect(page.locator("#result-group-position")).to_have_text("第 2/2 組")
    expect(page.locator("#next-result-group")).to_be_disabled()
    assert len(calls) == 2
    # 不重新載入即可切換偏好，CSS 與原生捲動的目的地仍一致。
    page.emulate_media(reduced_motion="reduce")
    page.locator("#previous-result-group").press("Enter")
    expect(page.locator("#result-group-position")).to_have_text("第 1/2 組")
    expect(page.locator("#previous-result-group")).to_be_disabled()
    assert page.evaluate(
        "/** reduced motion 返回第一組時軌道已回原點。 */ "
        "() => Math.abs(document.querySelector('#result-groups').scrollLeft) < 1"
    )
    assert len(calls) == 2


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_single_group_navigation_is_not_visible_or_focusable(
    engine_page: Page, base_url: str
) -> None:
    """hidden 導覽不能被 display:flex 蓋過，也不留下可操作的 Tab 停靠點。"""
    page = engine_page
    open_groups(page, base_url, 1)
    expect(page.locator("#results")).to_be_visible()
    expect(page.locator("#result-group-navigation")).to_be_hidden()
    expect(page.get_by_role("button", name="上一組結果")).to_have_count(0)
    expect(page.get_by_role("button", name="下一組結果")).to_have_count(0)


@pytest.mark.parametrize("engine_page", ["chromium", "firefox"], indirect=True)
def test_single_replacement_hides_previous_batch_navigation(
    engine_page: Page, base_url: str
) -> None:
    """多組結果替換為單筆後，不保留上一輪群組導覽或發出額外請求。"""
    page = engine_page
    calls = open_groups(page, base_url, 2)
    expect(page.locator("#result-group-navigation")).to_be_visible()
    page.locator("#single-mode-button").click()
    page.locator("#post-url").fill("https://x.com/creator/status/3")
    page.locator("#analyze-button").click()
    expect(page.locator(".result-group")).to_have_count(1)
    expect(page.locator("#result-group-navigation")).to_be_hidden()
    assert calls == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/3",
    ]
