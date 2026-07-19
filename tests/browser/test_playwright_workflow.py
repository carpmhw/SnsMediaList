"""Mocked browser tests for the contact-grid workflow."""

from __future__ import annotations

import json
from collections.abc import Generator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

STATIC_DIR = Path(__file__).parents[2] / "src" / "sns_media_list" / "static"
SUCCESS_PAYLOAD: dict[str, Any] = {
    "platform": "x",
    "post_url": "https://x.com/creator/status/1",
    "author": "creator",
    "description": "A test post",
    "unavailable_media_count": 1,
    "media": [
        {
            "token": "opaque-download",
            "media_type": "image",
            "filename": "x-1-1.jpg",
            "width": 1200,
            "height": 800,
            "duration": None,
            "preview_url": None,
            "download_url": "/api/media/opaque-download/download",
        }
    ],
}


class QuietStaticHandler(SimpleHTTPRequestHandler):
    """Serve static files without writing access logs into test output."""

    def log_message(self, format: str, *_args: object) -> None:
        """Suppress access logging for the local browser server."""


@pytest.fixture
def base_url() -> Generator[str, None, None]:
    """Serve the static client from an ephemeral local HTTP port."""
    handler = partial(QuietStaticHandler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def page() -> Generator[Page, None, None]:
    """Launch a headless Chromium page for one isolated browser test."""
    with sync_playwright() as playwright:
        browser = None
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            pytest.skip(f"Chromium is unavailable: {error}")
        assert browser is not None
        browser_page = browser.new_page(accept_downloads=True)
        try:
            yield browser_page
        finally:
            browser.close()


def fulfill_json(route: Any, payload: dict[str, Any], *, status: int = 200) -> None:
    """Fulfill a mocked browser request with a JSON response."""
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def test_submits_and_replaces_results(page: Page, base_url: str) -> None:
    """Verify loading, metadata, warning, and replacement of the card grid."""
    calls: list[dict[str, str]] = []
    first_payload = {
        **SUCCESS_PAYLOAD,
        "media": [SUCCESS_PAYLOAD["media"][0], {**SUCCESS_PAYLOAD["media"][0], "token": "second"}],
    }

    def extraction(route: Any, request: Any) -> None:
        """Return two items for the first request and one for the second."""
        calls.append(json.loads(request.post_data or "{}"))
        fulfill_json(route, first_payload if len(calls) == 1 else SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator("#status")).to_contain_text("準備就緒")
    expect(page.locator("#results-summary")).to_contain_text("2 個媒體項目")
    expect(page.locator("#unavailable-warning")).to_be_visible()
    expect(page.locator(".media-card")).to_have_count(2)

    page.fill("#post-url", "https://x.com/creator/status/2")
    page.click("#analyze-button")

    expect(page.locator("#results-summary")).to_contain_text("1 個媒體項目已準備就緒")
    expect(page.locator(".media-card")).to_have_count(1)
    assert calls == [
        {"url": "https://x.com/creator/status/1"},
        {"url": "https://x.com/creator/status/2"},
    ]


def test_rate_limit_error_is_inline(page: Page, base_url: str) -> None:
    """Verify stable API errors appear in the status region."""

    def extraction_error(route: Any, _request: Any) -> None:
        """Return a stable upstream rate-limit error."""
        fulfill_json(
            route,
            {"code": "upstream_rate_limited", "message": "hidden raw detail", "request_id": "test"},
            status=429,
        )

    page.route("**/api/extractions", extraction_error)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator("#status")).to_contain_text("平台暫時限制匿名存取")
    expect(page.locator("#status")).not_to_contain_text("hidden raw detail")


def test_expired_download_offers_reanalysis(page: Page, base_url: str) -> None:
    """Verify expired download tokens can recover using the original URL."""
    extraction_calls: list[dict[str, str]] = []

    def extraction(route: Any, request: Any) -> None:
        """Return the same result while recording recovery submissions."""
        extraction_calls.append(json.loads(request.post_data or "{}"))
        fulfill_json(route, SUCCESS_PAYLOAD)

    def expired_download(route: Any, _request: Any) -> None:
        """Return the stable expired-token error for a download."""
        fulfill_json(
            route,
            {"code": "token_expired", "message": "hidden raw detail", "request_id": "test"},
            status=410,
        )

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", expired_download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    page.click(".download-action")

    expect(page.locator(".re-analyze")).to_be_visible()
    expect(page.locator("#status")).to_contain_text("下載參照已過期")
    page.click(".re-analyze")

    expect(page.locator("#status")).to_contain_text("準備就緒")
    assert extraction_calls == [
        {"url": "https://x.com/creator/status/1"},
        {"url": "https://x.com/creator/status/1"},
    ]


def test_download_requests_only_after_click(page: Page, base_url: str) -> None:
    """Verify fallback cards avoid media fetches until Download is clicked."""
    download_calls: list[str] = []

    def extraction(route: Any, _request: Any) -> None:
        """Return a media item without a preview URL."""
        fulfill_json(route, SUCCESS_PAYLOAD)

    def download(route: Any, request: Any) -> None:
        """Return a tiny JPEG body and record the request path."""
        download_calls.append(request.url)
        route.fulfill(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff\xe0test")

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator(".fallback-tile")).to_contain_text("找不到預覽")
    assert page.locator("video").count() == 0
    assert download_calls == []
    page.click(".download-action")
    expect(page.locator("#status")).to_contain_text("已開始下載")
    assert len(download_calls) == 1


def test_mobile_layout_has_no_horizontal_overflow_and_supports_keyboard(
    page: Page, base_url: str
) -> None:
    """Verify the mobile layout is one column and keyboard operable."""
    extraction_calls: list[dict[str, str]] = []

    def extraction(route: Any, request: Any) -> None:
        """Return a deterministic result for keyboard submission."""
        extraction_calls.append(json.loads(request.post_data or "{}"))
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(base_url)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id === 'analyze-button'")
    page.keyboard.press("Enter")

    expect(page.locator(".media-grid")).to_be_visible()
    column_count = page.locator(".media-grid").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
    )
    assert column_count == 1
    assert extraction_calls == [{"url": "https://x.com/creator/status/1"}]


def test_reduced_motion_and_external_asset_boundaries(page: Page, base_url: str) -> None:
    """Verify reduced motion and same-origin static asset constraints."""
    page.emulate_media(reduced_motion="reduce")
    page.goto(base_url)

    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.locator("script[src^='http']").count() == 0
    assert page.locator("link[href^='http']").count() == 0
