"""以實際渲染顏色驗證按鈕對比、狀態及既有版面。"""

import base64

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.browser import test_playwright_workflow as workflow
from tests.browser.test_media_viewer import open_results

page = workflow.page
touch_page = workflow.touch_page
base_url = workflow.base_url
download_request_methods = workflow.download_request_methods


def settle_styles(control: Locator) -> None:
    """等待目前控制的真實 transition 完成，不停用動畫或使用固定 sleep。"""
    control.evaluate(
        """/** 強制樣式更新後等待目前控制的有限動畫。 */ async el => {
          getComputedStyle(el).backgroundColor;
          let animations = el.getAnimations();
          while (animations.length) {
            await Promise.allSettled(animations.map(
              /** 觸控釋放可能取消 active transition，取消亦視為該動畫已結束。 */
              animation => animation.finished));
            getComputedStyle(el).backgroundColor;
            animations = el.getAnimations();
          }
        }"""
    )


def text_contrast(control: Locator) -> float:
    """以 computed 前景與實際背景像素量測最差對比，包含透明及祖先漸層。"""
    settle_styles(control)
    foreground: list[float] = control.evaluate(
        r"""/** 取得瀏覽器已解析的 RGB／RGBA 前景。 */ el => {
          return getComputedStyle(el).color.match(/[\d.]+/g).map(Number);
        }"""
    )
    original_style = control.get_attribute("style")
    try:
        control.evaluate(
            """/** 僅在擷取背景時隱藏字色，不改尺寸、背景或動畫。 */ el => {
              el.style.setProperty('color', 'transparent', 'important');
              el.style.setProperty('text-decoration-color', 'transparent', 'important');
            }"""
        )
        screenshot = control.screenshot(animations="allow")
    finally:
        if original_style is None:
            control.evaluate(
                "/** 還原原本沒有 inline style 的控制。 */ el => el.removeAttribute('style')"
            )
        else:
            control.evaluate(
                """/** 還原原有 inline style，不覆蓋測試外的設定。 */
                (el, style) => el.setAttribute('style', style)""",
                original_style,
            )
    return float(
        control.page.evaluate(
            """/** 由瀏覽器解碼背景 PNG，量測中央區每個像素的最差文字對比。 */
        async ({ png, foreground }) => {
          const bytes = Uint8Array.from(atob(png),
            /** 將 base64 解碼的字元還原成 PNG bytes。 */ char => char.charCodeAt(0));
          const image = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
          const canvas = document.createElement('canvas');
          canvas.width = image.width;
          canvas.height = image.height;
          const context = canvas.getContext('2d');
          context.drawImage(image, 0, 0);
          image.close();
          const insetX = Math.floor(canvas.width / 4);
          const insetY = Math.floor(canvas.height / 4);
          const pixels = context.getImageData(insetX, insetY,
            canvas.width - 2 * insetX, canvas.height - 2 * insetY).data;
          /** 依 WCAG sRGB 線性化公式求相對亮度。 */
          const luminance = channels => {
            const linear = channels.map(
              /** 將單一 sRGB channel 線性化。 */ value => value <= 0.04045
                ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
            return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
          };
          const alpha = foreground.length === 4 ? foreground[3] : 1;
          let worst = Infinity;
          for (let i = 0; i < pixels.length; i += 4) {
            const background = [pixels[i] / 255, pixels[i + 1] / 255, pixels[i + 2] / 255];
            const visible = background.map(
              /** 將半透明字色合成到真正背景上。 */ (value, index) =>
                foreground[index] / 255 * alpha + value * (1 - alpha));
            const a = luminance(visible);
            const b = luminance(background);
            worst = Math.min(worst, (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05));
          }
          return worst;
        }""",
            {"png": base64.b64encode(screenshot).decode("ascii"), "foreground": foreground},
        )
    )


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_mode_buttons_remain_readable(page: Page, base_url: str, mode: str) -> None:
    """切換單筆或批次後，選中與非選中按鈕在 hover 前後都可讀。"""
    page.goto(base_url)
    page.locator(f"#{mode}-mode-button").click()
    backgrounds = []
    for name in ["single", "batch"]:
        control = page.locator(f"#{name}-mode-button")
        expected_state = "true" if name == mode else "false"
        page.mouse.move(0, 0)
        assert text_contrast(control) >= 4.5
        control.hover()
        assert text_contrast(control) >= 4.5
        expect(control).to_have_attribute("aria-pressed", expected_state)
        backgrounds.append(
            control.evaluate(
                "/** 讀取目前模式背景以比較狀態。 */ el => getComputedStyle(el).backgroundColor"
            )
        )
    assert backgrounds[0] != backgrounds[1]


def test_secondary_controls_remain_readable(page: Page, base_url: str) -> None:
    """卡片次要操作及失效授權的恢復入口不被全域 hover 蓋成亮底。"""
    open_results(page, base_url)
    for selector in [".media-view", ".copy-filename", ".download-action"]:
        control = page.locator(selector).first
        page.mouse.move(0, 0)
        assert text_contrast(control) >= 4.5, selector
        control.hover()
        assert text_contrast(control) >= 4.5, selector

    def expired_authorization(route: Route) -> None:
        """以合成過期錯誤走既有全頁恢復狀態，不連外。"""
        route.fulfill(
            status=410,
            json={
                "code": "token_expired",
                "message": "safe fixture",
                "request_id": "test",
            },
        )

    page.route("**/api/extractions", expired_authorization)
    page.locator("#analyze-button").click()
    recovery = page.locator("#status .re-analyze")
    expect(recovery).to_be_visible()
    page.mouse.move(0, 0)
    assert text_contrast(recovery) >= 4.5
    recovery.hover()
    assert text_contrast(recovery) >= 4.5


def test_primary_hover_and_disabled_state(page: Page, base_url: str) -> None:
    """主操作維持可讀；真實分析中停用的按鈕不呈現啟用 hover。"""
    page.goto(base_url)
    control = page.locator("#analyze-button")
    assert text_contrast(control) >= 4.5
    control.hover()
    assert text_contrast(control) >= 4.5
    page.mouse.move(0, 0)

    def pending(route: Route) -> None:
        """暫緩分析回應，維持真實 loading／disabled 狀態供量測。"""
        held.append(route)

    held: list[Route] = []
    page.route("**/api/extractions", pending)
    page.locator("#post-url").fill("https://x.com/creator/status/1")
    control.click()
    expect(control).to_be_disabled()
    page.mouse.move(0, 0)
    settle_styles(control)
    before = control.evaluate(
        """/** 保存停用狀態的背景與位移。 */ el => {
          const style = getComputedStyle(el);
          return [style.backgroundColor, style.transform];
        }"""
    )
    control.hover()
    settle_styles(control)
    after = control.evaluate(
        """/** 檢查滑鼠留在停用控制上未改變狀態。 */ el => {
          const style = getComputedStyle(el);
          return [style.backgroundColor, style.transform];
        }"""
    )
    assert after == before
    expect(control).to_be_disabled()
    for route in held:
        route.abort()


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
def test_contrast_fix_preserves_keyboard_and_layout(page: Page, base_url: str, width: int) -> None:
    """鍵盤切換模式仍有可見焦點，各螢幕寬度的結果區不水平溢出。"""
    page.set_viewport_size({"width": width, "height": 900})
    open_results(page, base_url)
    page.locator("#single-mode-button").focus()
    page.keyboard.press("Tab")
    control = page.locator("#batch-mode-button")
    expect(control).to_be_focused()
    assert control.evaluate(
        "/** 檢查鍵盤焦點指示沒有被 hover 規則移除。 */ el => el.matches(':focus-visible')"
    )
    outline = control.evaluate(
        """/** 讀取真實焦點外框樣式。 */ el => [
          getComputedStyle(el).outlineStyle, parseFloat(getComputedStyle(el).outlineWidth)]"""
    )
    assert outline[0] != "none" and outline[1] >= 2
    page.keyboard.press("Enter")
    expect(control).to_have_attribute("aria-pressed", "true")
    assert page.evaluate(
        """/** 檢查窄版與桌面皆不產生整頁水平溢出。 */
        () => document.documentElement.scrollWidth <= innerWidth"""
    )


def test_touch_mode_switch_stays_operable(touch_page: Page, base_url: str) -> None:
    """375 寬觸控模擬仍可切換模式，不將黏住的 hover 當作選中狀態。"""
    touch_page.goto(base_url)
    for name in ["batch", "single"]:
        control = touch_page.locator(f"#{name}-mode-button")
        control.tap()
        expect(control).to_have_attribute("aria-pressed", "true")
        expect(
            touch_page.locator(f"#{'single' if name == 'batch' else 'batch'}-mode-button")
        ).to_have_attribute("aria-pressed", "false")
        assert text_contrast(control) >= 4.5
