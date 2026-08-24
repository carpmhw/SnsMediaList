"""Mocked browser tests for the contact-grid workflow."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Generator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

STATIC_DIR = Path(__file__).parents[2] / "src" / "sns_media_list" / "static"
PREVIEW_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
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
X_VIDEO_FILENAME = "x-1-1.mp4"
X_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x0cmdatSNSM"
X_VIDEO_PAYLOAD: dict[str, Any] = {
    **SUCCESS_PAYLOAD,
    "description": "A deterministic X video",
    "unavailable_media_count": 0,
    "media": [
        {
            "token": "opaque-x-video-download",
            "media_type": "video",
            "filename": X_VIDEO_FILENAME,
            "width": 1920,
            "height": 1080,
            "duration": 12.5,
            "preview_url": None,
            "download_url": "/api/media/opaque-x-video-download/download",
        }
    ],
}


class PreviewServer:
    """Serve controlled preview responses while recording request concurrency."""

    def __init__(self) -> None:
        """Start a threaded local server for preview response timing tests."""
        self._lock = Lock()
        self._active = 0
        self._max_active = 0
        self._started: list[str] = []
        self._completed: list[str] = []
        self._attempts: dict[str, int] = {}
        self._attempt_times: dict[str, list[float]] = {}
        self.failures: dict[str, int] = {}
        self.blocked_tokens: set[str] = set()
        self.release = Event()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(PreviewRequestHandler, state=self),
        )
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """Return the local server origin used by Playwright route rewrites."""
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def max_active(self) -> int:
        """Return the greatest number of simultaneous preview requests."""
        with self._lock:
            return self._max_active

    @property
    def started_tokens(self) -> list[str]:
        """Return preview tokens in the order their requests reached the server."""
        with self._lock:
            return list(self._started)

    @property
    def completed_tokens(self) -> list[str]:
        """Return preview tokens in the order their responses completed."""
        with self._lock:
            return list(self._completed)

    def attempts_for(self, token: str) -> int:
        """Return the number of requests received for one preview token."""
        with self._lock:
            return self._attempts.get(token, 0)

    def attempt_times_for(self, token: str) -> list[float]:
        """Return monotonic start times for one preview token's requests."""
        with self._lock:
            return list(self._attempt_times.get(token, []))

    def begin(self, token: str) -> int:
        """Record one request and return its one-based attempt number."""
        with self._lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
            self._started.append(token)
            attempt = self._attempts.get(token, 0) + 1
            self._attempts[token] = attempt
            self._attempt_times.setdefault(token, []).append(time.monotonic())
            return attempt

    def end(self, token: str) -> None:
        """Record completion of one preview request."""
        with self._lock:
            self._active = max(0, self._active - 1)
            self._completed.append(token)

    def close(self) -> None:
        """Stop the preview server and release any deliberately blocked requests."""
        self.release.set()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    """Return deterministic image or failure responses for preview tests."""

    def __init__(self, request: Any, client_address: Any, server: Any, *, state: PreviewServer):
        """Attach the shared preview state before initializing the HTTP handler."""
        self.state = state
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        """Serve one preview response with configured failures or blocking."""
        token = self.path.rstrip("/").split("/")[-2]
        attempt = self.state.begin(token)
        try:
            if token in self.state.blocked_tokens:
                self.state.release.wait(timeout=5)
            time.sleep(0.05)
            if attempt <= self.state.failures.get(token, 0):
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                body = b"temporary preview failure"
            else:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                body = PREVIEW_BYTES
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            self.state.end(token)

    def log_message(self, format: str, *_args: object) -> None:
        """Suppress access logging for the controlled preview server."""


def route_preview_to_server(route: Any, request: Any, preview_server: PreviewServer) -> None:
    """Rewrite opaque preview paths to the controlled local response server."""
    route.continue_(url=f"{preview_server.url}{urlsplit(request.url).path}")


def build_preview_payload(paths: list[str], *, post_number: str = "1") -> dict[str, Any]:
    """Build an extraction payload containing one opaque preview per media item."""
    media = []
    for index, preview_url in enumerate(paths, start=1):
        media.append(
            {
                **SUCCESS_PAYLOAD["media"][0],
                "token": f"download-{post_number}-{index}",
                "filename": f"x-{post_number}-{index}.jpg",
                "preview_url": preview_url,
                "download_url": f"/api/media/download-{post_number}-{index}/download",
            }
        )
    return {
        **SUCCESS_PAYLOAD,
        "post_url": f"https://x.com/creator/status/{post_number}",
        "description": f"Preview test {post_number}",
        "unavailable_media_count": 0,
        "media": media,
    }


def wait_for_condition(
    page: Page,
    condition: Callable[[], bool],
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Poll a threaded test condition while allowing Playwright events to run."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return
        page.wait_for_timeout(20)
    raise AssertionError("timed out waiting for preview server condition")


def wait_for_loaded_previews(page: Page, count: int) -> None:
    """Wait until every expected preview image has decoded successfully."""
    page.wait_for_function(
        """count => {
          const images = [...document.querySelectorAll('.media-visual img')];
          return images.length === count && images.every(
            image => image.complete && image.naturalWidth > 0
          );
        }""",
        arg=count,
        timeout=5000,
    )


class ExtractionServer:
    """Serve overlapping extraction responses with a controllable first response."""

    def __init__(self, payloads: list[dict[str, Any]], statuses: list[int] | None = None) -> None:
        """Start a threaded extraction server using payloads in request order."""
        self._lock = Lock()
        self.payloads = payloads
        self.statuses = statuses or [200]
        self.requests: list[str] = []
        self.first_started = Event()
        self.release_first = Event()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(ExtractionRequestHandler, state=self),
        )
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """Return the local extraction server origin."""
        return f"http://127.0.0.1:{self.server.server_port}"

    def response_for(self, url: str) -> tuple[int, dict[str, Any]]:
        """Record one extraction URL and return its ordered status and payload."""
        with self._lock:
            index = len(self.requests)
            self.requests.append(url)
        if index == 0:
            self.first_started.set()
            self.release_first.wait(timeout=5)
        return (
            self.statuses[min(index, len(self.statuses) - 1)],
            self.payloads[min(index, len(self.payloads) - 1)],
        )

    def close(self) -> None:
        """Stop the extraction server and release its delayed response."""
        self.release_first.set()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class ExtractionRequestHandler(SimpleHTTPRequestHandler):
    """Decode one extraction request and return a controlled JSON payload."""

    def __init__(self, request: Any, client_address: Any, server: Any, *, state: ExtractionServer):
        """Attach the shared extraction state before initializing the handler."""
        self.state = state
        super().__init__(request, client_address, server)

    def do_POST(self) -> None:
        """Return the next extraction payload after recording its requested URL."""
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        status, response_payload = self.state.response_for(str(payload.get("url", "")))
        body = json.dumps(response_payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *_args: object) -> None:
        """Suppress access logging for the controlled extraction server."""


def route_extraction_to_server(route: Any, request: Any, server: ExtractionServer) -> None:
    """Rewrite extraction requests to the controlled local response server."""
    route.continue_(url=f"{server.url}{urlsplit(request.url).path}")


STORY_URL = "https://www.instagram.com/stories/example.user/1111111111111111111/"
STORY_SUCCESS_PAYLOAD: dict[str, Any] = {
    "platform": "instagram",
    "post_url": STORY_URL,
    "author": "example.user",
    "description": "An exact Story",
    "unavailable_media_count": 0,
    "media": [
        {
            "token": "opaque-story-download",
            "media_type": "image",
            "filename": "instagram-1111111111111111111-1.jpg",
            "width": 1080,
            "height": 1920,
            "duration": None,
            "preview_url": None,
            "download_url": "/api/media/opaque-story-download/download",
        }
    ],
}


class QuietStaticHandler(SimpleHTTPRequestHandler):
    """Serve static files without writing access logs into test output."""

    def __init__(
        self,
        request: Any,
        client_address: Any,
        server: Any,
        *,
        download_request_methods: list[str],
        directory: str,
    ) -> None:
        """保存下載 method 紀錄並初始化靜態檔案 handler。"""
        self.download_request_methods = download_request_methods
        super().__init__(request, client_address, server, directory=directory)

    def do_HEAD(self) -> None:
        """以 204 回應 token 下載預檢並記錄 HEAD。"""
        if urlsplit(self.path).path != X_VIDEO_PAYLOAD["media"][0]["download_url"]:
            super().do_HEAD()
            return
        self.download_request_methods.append("HEAD")
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        """以 attachment 200 回應 token 下載並記錄 GET。"""
        if urlsplit(self.path).path != X_VIDEO_PAYLOAD["media"][0]["download_url"]:
            super().do_GET()
            return
        self.download_request_methods.append("GET")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition", f'attachment; filename="{X_VIDEO_FILENAME}"')
        self.send_header("Content-Length", str(len(X_VIDEO_BYTES)))
        self.end_headers()
        self.wfile.write(X_VIDEO_BYTES)

    def log_message(self, format: str, *_args: object) -> None:
        """Suppress access logging for the local browser server."""


@pytest.fixture
def download_request_methods() -> list[str]:
    """提供每個 browser test 隔離的下載 method 紀錄。"""
    return []


@pytest.fixture
def base_url(download_request_methods: list[str]) -> Generator[str, None, None]:
    """從 ephemeral 本機 HTTP port 提供靜態 client 與 mock 下載。"""
    handler = partial(
        QuietStaticHandler,
        directory=str(STATIC_DIR),
        download_request_methods=download_request_methods,
    )
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


@pytest.fixture
def touch_page() -> Generator[Page, None, None]:
    """建立具觸控能力的 Chromium context，供水平滑動互動測試使用。"""
    with sync_playwright() as playwright:
        browser = None
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            pytest.skip(f"Chromium is unavailable: {error}")
        assert browser is not None
        context = browser.new_context(viewport={"width": 375, "height": 800}, has_touch=True)
        browser_page = context.new_page()
        try:
            yield browser_page
        finally:
            context.close()
            browser.close()


def fulfill_json(route: Any, payload: dict[str, Any], *, status: int = 200) -> None:
    """Fulfill a mocked browser request with a JSON response."""
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


@pytest.fixture
def preview_server() -> Generator[PreviewServer, None, None]:
    """Provide a controlled threaded response server for preview lifecycle tests."""
    server = PreviewServer()
    try:
        yield server
    finally:
        server.close()


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
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator("#status")).to_contain_text("準備就緒")
    expect(page.locator("#results-summary")).to_contain_text("2 個媒體項目")
    expect(page.locator("#unavailable-warning")).to_be_visible()
    expect(page.locator(".media-card")).to_have_count(2)
    multi_column_count = page.locator(".media-grid").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
    )
    assert multi_column_count >= 2

    page.fill("#post-url", "https://x.com/creator/status/2")
    page.click("#analyze-button")

    expect(page.locator("#results-summary")).to_contain_text("1 個媒體項目已準備就緒")
    expect(page.locator(".media-card")).to_have_count(1)
    expect(page.locator(".selection-toolbar")).to_have_count(1)
    grid_box = page.locator(".media-grid").bounding_box()
    card_box = page.locator(".media-card").bounding_box()
    assert grid_box is not None
    assert card_box is not None
    assert card_box["width"] <= 480
    grid_center = grid_box["x"] + grid_box["width"] / 2
    card_center = card_box["x"] + card_box["width"] / 2
    assert abs(grid_center - card_center) <= 1
    assert calls == [
        {"url": "https://x.com/creator/status/1"},
        {"url": "https://x.com/creator/status/2"},
    ]


def test_multiple_token_previews_are_loaded_one_at_a_time(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify a multi-item result never creates more than one preview request at once."""
    payload = build_preview_payload(
        [
            "/api/media/preview-one/preview",
            "/api/media/preview-two/preview",
            "/api/media/preview-three/preview",
        ]
    )

    def extraction(route: Any, _request: Any) -> None:
        """Return three token-bound previews for the concurrency regression case."""
        fulfill_json(route, payload)

    def preview(route: Any, request: Any) -> None:
        """Rewrite one browser preview request to the delayed local server."""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    wait_for_loaded_previews(page, 3)

    assert preview_server.max_active == 1
    assert preview_server.started_tokens == ["preview-one", "preview-two", "preview-three"]
    assert preview_server.completed_tokens == ["preview-one", "preview-two", "preview-three"]


def test_preview_retries_once_after_an_initial_load_error(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify one transient preview failure is retried after the configured delay."""
    token = "preview-retry"
    preview_server.failures[token] = 1
    payload = build_preview_payload([f"/api/media/{token}/preview"])

    def extraction(route: Any, _request: Any) -> None:
        """Return one preview that fails once before succeeding."""
        fulfill_json(route, payload)

    def preview(route: Any, request: Any) -> None:
        """Rewrite the retry case to the controlled preview server."""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    wait_for_loaded_previews(page, 1)

    attempt_times = preview_server.attempt_times_for(token)
    assert len(attempt_times) == 2
    assert attempt_times[1] - attempt_times[0] >= 0.9
    assert preview_server.attempts_for(token) == 2
    assert page.locator(".fallback-tile").count() == 0


def test_preview_fallback_keeps_download_available_after_two_failures(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify an exhausted preview retry budget does not disable the download action."""
    token = "preview-dead"
    preview_server.failures[token] = 2
    payload = build_preview_payload([f"/api/media/{token}/preview"])
    download_calls: list[str] = []

    def extraction(route: Any, _request: Any) -> None:
        """Return one preview that remains unavailable on both attempts."""
        fulfill_json(route, payload)

    def preview(route: Any, request: Any) -> None:
        """Rewrite the permanently failing preview to the controlled server."""
        route_preview_to_server(route, request, preview_server)

    def download(route: Any, request: Any) -> None:
        """Serve the token-bound download preflight and attachment response."""
        download_calls.append(request.method)
        if request.method == "HEAD":
            route.fulfill(status=204)
        else:
            route.fulfill(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff\xe0test")

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.route("**/api/media/download-1-1/download", download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    wait_for_condition(page, lambda: preview_server.attempts_for(token) == 2)
    expect(page.locator(".fallback-tile")).to_have_count(1)
    assert page.locator(".download-action").is_enabled()
    page.click(".download-action")

    expect(page.locator("#status")).to_contain_text("已開始下載")
    assert download_calls[0] == "HEAD"


def test_fixed_placeholder_bypasses_preview_queue(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify a local placeholder does not become an opaque preview request."""
    payload = build_preview_payload(
        [
            "/placeholder.svg",
            "/api/media/remote-one/preview",
            "/api/media/remote-two/preview",
        ]
    )

    def extraction(route: Any, _request: Any) -> None:
        """Return one fixed placeholder and two remote previews."""
        fulfill_json(route, payload)

    def preview(route: Any, request: Any) -> None:
        """Rewrite only opaque preview paths to the controlled server."""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    wait_for_loaded_previews(page, 3)

    assert preview_server.started_tokens == ["remote-one", "remote-two"]


def test_replacing_results_cancels_active_queued_and_delayed_preview_work(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify stale preview callbacks cannot continue after a result replacement."""
    retry_token = "old-retry"
    active_token = "old-active"
    queued_token = "old-never"
    preview_server.failures[retry_token] = 1
    preview_server.blocked_tokens.add(active_token)
    old_payload = build_preview_payload(
        [
            f"/api/media/{retry_token}/preview",
            f"/api/media/{active_token}/preview",
            f"/api/media/{queued_token}/preview",
        ],
        post_number="old",
    )
    new_token = "new-preview"
    new_payload = build_preview_payload([f"/api/media/{new_token}/preview"], post_number="new")
    extraction_calls: list[str] = []

    def extraction(route: Any, request: Any) -> None:
        """Return old preview work first and a replacement result second."""
        extraction_calls.append(json.loads(request.post_data or "{}")["url"])
        fulfill_json(route, old_payload if len(extraction_calls) == 1 else new_payload)

    def preview(route: Any, request: Any) -> None:
        """Rewrite old and new preview requests to the controlled server."""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/old")
    page.click("#analyze-button")

    wait_for_condition(
        page,
        lambda: preview_server.attempts_for(retry_token) == 1
        and preview_server.attempts_for(active_token) == 1,
    )
    assert preview_server.attempts_for(queued_token) == 0

    page.fill("#post-url", "https://x.com/creator/status/new")
    page.click("#analyze-button")
    wait_for_condition(page, lambda: preview_server.attempts_for(new_token) == 1)
    wait_for_loaded_previews(page, 1)
    preview_server.release.set()
    page.wait_for_timeout(1200)

    assert extraction_calls == [
        "https://x.com/creator/status/old",
        "https://x.com/creator/status/new",
    ]
    assert preview_server.attempts_for(retry_token) == 1
    assert preview_server.attempts_for(queued_token) == 0
    assert page.locator(".media-card").count() == 1
    expect(page.locator(".media-visual .media-metadata")).to_contain_text("項目")


def test_unresolved_single_analysis_ignores_a_second_single_submit(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """驗證未完成的單筆分析會忽略第二次 submit，且原請求可正常完成。"""
    old_payload = build_preview_payload(
        [
            "/api/media/old-one/preview",
            "/api/media/old-two/preview",
        ],
        post_number="old",
    )
    extraction_server = ExtractionServer([old_payload])

    def extraction(route: Any, request: Any) -> None:
        """將單筆 extraction 請求導向可延遲的本機 server。"""
        route_extraction_to_server(route, request, extraction_server)

    def preview(route: Any, request: Any) -> None:
        """Rewrite result previews to the controlled local preview server."""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/old")
    page.click("#analyze-button")
    wait_for_condition(page, lambda: len(extraction_server.requests) == 1)

    page.fill("#post-url", "https://x.com/creator/status/new")
    page.evaluate("document.querySelector('#extraction-form').requestSubmit()")
    page.wait_for_timeout(150)
    assert extraction_server.requests == ["https://x.com/creator/status/old"]

    extraction_server.release_first.set()
    expect(page.locator("#results-summary")).to_contain_text("2 個媒體項目已準備就緒")
    wait_for_loaded_previews(page, 2)

    assert extraction_server.requests == ["https://x.com/creator/status/old"]
    expect(page.locator("#source-link")).to_have_attribute(
        "href", "https://x.com/creator/status/old"
    )
    assert preview_server.attempts_for("old-one") == 1


def test_story_submission_uses_existing_workflow(page: Page, base_url: str) -> None:
    """Verify an exact Story uses the shared extraction form, endpoint, and result grid."""
    submission_urls: list[str] = []

    def record_submission(request: Any) -> None:
        """Record every browser submission so Story-specific endpoints cannot be hidden."""
        if request.method == "POST":
            submission_urls.append(request.url)

    def extraction(route: Any, request: Any) -> None:
        """Validate the shared extraction request and return one Story image."""
        assert request.method == "POST"
        assert json.loads(request.post_data or "{}") == {"url": STORY_URL}
        fulfill_json(route, STORY_SUCCESS_PAYLOAD)

    page.on("request", record_submission)
    page.route("**/api/extractions", extraction)
    page.goto(base_url)

    expect(page.locator("form")).to_have_count(1)
    expect(page.locator("#extraction-form #post-url")).to_have_count(1)
    expect(page.locator("#extraction-form #analyze-button")).to_have_count(1)
    expect(page.get_by_role("radio")).to_have_count(0)
    expect(page.get_by_role("combobox")).to_have_count(0)
    page.fill("#post-url", STORY_URL)
    page.click("#analyze-button")

    expect(page.locator("#results")).to_be_visible()
    expect(page.locator(".media-card")).to_have_count(1)
    expect(page.locator(".media-card")).to_be_visible()
    expect(page.locator(".media-metadata")).to_contain_text("圖片")
    expect(page.locator("#source-link")).to_be_visible()
    expect(page.locator("#source-link")).to_have_attribute("href", STORY_URL)
    assert submission_urls == [f"{base_url}/api/extractions"]


def test_story_unavailable_uses_safe_generic_message(page: Page, base_url: str) -> None:
    """Verify unavailable Stories show safe copy without raw or inferred reasons."""
    raw_message = "Story expired, was deleted, or is unavailable because of permissions."

    def extraction_error(route: Any, request: Any) -> None:
        """Return the stable unavailable response for the shared Story submission."""
        assert request.method == "POST"
        assert json.loads(request.post_data or "{}") == {"url": STORY_URL}
        fulfill_json(
            route,
            {"code": "story_unavailable", "message": raw_message, "request_id": "test"},
            status=404,
        )

    page.route("**/api/extractions", extraction_error)
    page.goto(base_url)
    page.fill("#post-url", STORY_URL)
    page.click("#analyze-button")

    expect(page.locator("#status")).to_have_text("此 Story 目前無法使用。")
    expect(page.locator("#status")).not_to_contain_text(raw_message)
    for inferred_reason in ("過期", "刪除", "權限", "expired", "deleted", "permissions"):
        expect(page.locator("#status")).not_to_contain_text(inferred_reason)


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

    expect(page.locator("#status")).to_contain_text("平台暫時限制存取")
    expect(page.locator("#status")).not_to_contain_text("hidden raw detail")


def test_platform_authentication_error_is_operator_actionable(page: Page, base_url: str) -> None:
    """Verify platform session failures show safe operator guidance inline."""

    def extraction_error(route: Any, _request: Any) -> None:
        """Return a bounded platform authentication failure."""
        fulfill_json(
            route,
            {
                "code": "platform_authentication_failed",
                "message": "secret account and cookie path",
                "request_id": "test",
            },
            status=503,
        )

    page.route("**/api/extractions", extraction_error)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator("#status")).to_contain_text("平台驗證工作階段無法使用")
    expect(page.locator("#status")).to_contain_text("請聯絡服務管理者")
    expect(page.locator("#status")).not_to_contain_text("secret account and cookie path")


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


def test_x_video_download_saves_exact_attachment(
    page: Page,
    base_url: str,
    tmp_path: Path,
    download_request_methods: list[str],
) -> None:
    """驗證 X 影片只在點擊後依序預檢並下載完全相同的附件 bytes。"""

    def extraction(route: Any, _request: Any) -> None:
        """回傳沒有預覽且具有 token 下載 URL 的 deterministic X 影片。"""
        fulfill_json(route, X_VIDEO_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator(".fallback-tile")).to_contain_text("找不到預覽")
    assert page.locator("video").count() == 0
    assert download_request_methods == []
    with page.expect_download() as download_info:
        page.click(".download-action")
    browser_download = download_info.value
    saved_path = tmp_path / browser_download.suggested_filename
    browser_download.save_as(saved_path)

    expect(page.locator("#status")).to_have_text("已開始下載。")
    assert browser_download.suggested_filename == X_VIDEO_FILENAME
    assert saved_path.exists()
    assert saved_path.stat().st_size == len(X_VIDEO_BYTES)
    assert saved_path.read_bytes() == X_VIDEO_BYTES
    assert download_request_methods == ["HEAD", "GET"]


def test_group_selection_controls_and_download_in_source_order(page: Page, base_url: str) -> None:
    """Verify one result group owns selection and starts native downloads in source order."""
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {
                **SUCCESS_PAYLOAD["media"][0],
                "token": "first",
                "filename": "x-1-01.jpg",
                "download_url": "/api/media/first/download",
            },
            {
                **SUCCESS_PAYLOAD["media"][0],
                "token": "second",
                "media_type": "video",
                "filename": "x-1-02.mp4",
                "download_url": "/api/media/second/download",
            },
        ],
    }
    methods: list[str] = []

    def extraction(route: Any, _request: Any) -> None:
        """Return two deterministic downloadable media records."""
        fulfill_json(route, payload)

    def download(route: Any, request: Any) -> None:
        """Record preflights while allowing browser-native anchor navigation."""
        methods.append(f"{request.method}:{urlsplit(request.url).path}")
        if request.method == "HEAD":
            route.fulfill(status=204)
        else:
            route.fulfill(status=200, content_type="application/octet-stream", body=b"media")

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/download", download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator(".media-selection")).to_have_count(2)
    expect(page.get_by_role("button", name="下載選取項目")).to_be_disabled()
    page.locator(".media-selection").nth(1).check()
    expect(page.locator(".selection-summary")).to_contain_text("1 / 2")
    page.get_by_role("button", name="全選", exact=True).click()
    expect(page.get_by_role("button", name="下載選取項目")).to_be_enabled()
    page.get_by_role("button", name="下載選取項目").click()

    expect(page.locator(".download-summary")).to_contain_text("2 個下載已開始")
    assert methods[:2] == ["HEAD:/api/media/first/download", "HEAD:/api/media/second/download"]


def test_group_selection_filters_clear_and_replacement_reset(page: Page, base_url: str) -> None:
    """Verify group selection filters, clearing, and replacement never retain stale items."""
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {**SUCCESS_PAYLOAD["media"][0], "token": "image"},
            {
                **SUCCESS_PAYLOAD["media"][0],
                "token": "video",
                "media_type": "video",
                "filename": "x-1-02.mp4",
            },
        ],
    }
    requests = 0

    def extraction(route: Any, _request: Any) -> None:
        """Return a mixed-media result then a replacement result."""
        nonlocal requests
        requests += 1
        fulfill_json(route, payload)

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": 320, "height": 800})
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    page.get_by_role("button", name="只選圖片").click()
    expect(page.locator(".selection-summary")).to_contain_text("1 / 2")
    assert page.locator(".media-selection").nth(0).is_checked()
    assert not page.locator(".media-selection").nth(1).is_checked()
    page.get_by_role("button", name="只選影片").click()
    assert not page.locator(".media-selection").nth(0).is_checked()
    assert page.locator(".media-selection").nth(1).is_checked()
    page.get_by_role("button", name="取消全選").click()
    expect(page.locator(".selection-summary")).to_contain_text("0 / 2")
    expect(page.get_by_role("button", name="下載選取項目")).to_be_disabled()

    page.get_by_role("button", name="全選", exact=True).click()
    page.fill("#post-url", "https://x.com/creator/status/2")
    page.click("#analyze-button")

    expect(page.locator(".selection-summary")).to_contain_text("0 / 2")
    expect(page.get_by_role("button", name="下載選取項目")).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert requests == 2


def test_batch_download_paces_successes_and_continues_after_expired_token(
    page: Page, base_url: str
) -> None:
    """Verify selected downloads remain ordered, paced, and recover safely after one failure."""
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {
                **SUCCESS_PAYLOAD["media"][0],
                "token": token,
                "filename": f"x-1-{index:02d}.jpg",
                "download_url": f"/api/media/{token}/download",
            }
            for index, token in enumerate(("first", "expired", "third"), start=1)
        ],
    }
    requests: list[tuple[str, str, float]] = []

    def extraction(route: Any, _request: Any) -> None:
        """Return three source-ordered downloadable media records."""
        fulfill_json(route, payload)

    def download(route: Any, request: Any) -> None:
        """Preflight two items successfully and expire the middle download reference."""
        path = urlsplit(request.url).path
        requests.append((request.method, path, time.monotonic()))
        if request.method == "HEAD" and path == "/api/media/expired/download":
            fulfill_json(route, {"code": "token_expired"}, status=410)
        elif request.method == "HEAD":
            route.fulfill(status=204)
        else:
            route.fulfill(
                status=200,
                headers={"Content-Disposition": 'attachment; filename="media.jpg"'},
                body=b"media",
            )

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/download", download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    page.get_by_role("button", name="全選", exact=True).click()
    page.get_by_role("button", name="下載選取項目").click()

    expect(page.locator(".download-summary")).to_contain_text("瀏覽器可能需要允許多檔下載")
    expect(page.locator(".download-summary")).to_contain_text("2 個下載已開始，1 個無法啟動")
    expect(page.locator(".re-analyze")).to_be_visible()
    head_requests = [entry for entry in requests if entry[0] == "HEAD"]
    assert [entry[1] for entry in head_requests] == [
        "/api/media/first/download",
        "/api/media/expired/download",
        "/api/media/third/download",
    ]
    assert head_requests[1][2] - head_requests[0][2] >= 0.5
    assert "opaque" not in page.locator("#status").inner_text()


def test_selected_download_saves_exact_native_attachment(
    page: Page,
    base_url: str,
    tmp_path: Path,
    download_request_methods: list[str],
) -> None:
    """Verify selection downloads retain the API filename and complete attachment bytes."""

    def extraction(route: Any, _request: Any) -> None:
        """Return the deterministic video payload used by the local download server."""
        fulfill_json(route, X_VIDEO_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    page.locator(".media-selection").check()

    with page.expect_download() as download_info:
        page.get_by_role("button", name="下載選取項目").click()
    browser_download = download_info.value
    saved_path = tmp_path / browser_download.suggested_filename
    browser_download.save_as(saved_path)

    assert browser_download.suggested_filename == X_VIDEO_FILENAME
    assert saved_path.read_bytes() == X_VIDEO_BYTES
    expect(page.locator(".download-summary")).to_contain_text("1 個下載已開始，0 個無法啟動")
    assert download_request_methods == ["HEAD", "GET"]


def test_media_card_renders_available_structured_metadata(page: Page, base_url: str) -> None:
    """Verify cards expose safe platform and author metadata from the extraction payload."""

    def extraction(route: Any, _request: Any) -> None:
        """Return the complete image metadata fixture."""
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    metadata = page.locator(".media-metadata")
    expect(metadata).to_contain_text("平台")
    expect(metadata).to_contain_text("X")
    expect(metadata).to_contain_text("作者")
    expect(metadata).to_contain_text("creator")
    expect(metadata).to_contain_text("1200 × 800")
    expect(metadata).not_to_contain_text("undefined")


def test_media_metadata_is_an_accessible_visual_overlay(page: Page, base_url: str) -> None:
    """驗證結構化 metadata 位於縮圖浮層，並依桌機與手機互動條件顯示。"""

    def extraction(route: Any, _request: Any) -> None:
        """回傳含完整 metadata 的固定媒體項目。"""
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    card = page.locator(".media-card")
    visual = card.locator(".media-visual")
    metadata = visual.locator(".media-metadata")
    expect(metadata).to_contain_text("項目")
    expect(metadata).to_contain_text("類型")
    expect(metadata).to_contain_text("格式")
    expect(metadata).to_contain_text("尺寸")
    expect(metadata).to_contain_text("平台")
    expect(metadata).to_contain_text("作者")
    expect(card.locator(".media-body .media-metadata")).to_have_count(0)
    assert metadata.evaluate("element => getComputedStyle(element).opacity") == "0"

    visual.hover()
    page.wait_for_timeout(200)
    assert metadata.evaluate("element => getComputedStyle(element).opacity") == "1"

    card.locator(".media-selection").focus()
    page.wait_for_timeout(200)
    assert metadata.evaluate("element => getComputedStyle(element).opacity") == "1"

    page.set_viewport_size({"width": 640, "height": 900})
    assert metadata.evaluate("element => getComputedStyle(element).opacity") == "1"


def test_media_metadata_omits_invalid_values_and_formats_zero_duration(
    page: Page, base_url: str
) -> None:
    """Verify unavailable metadata is omitted while a valid zero video duration remains visible."""
    payload = {
        **X_VIDEO_PAYLOAD,
        "author": None,
        "media": [{**X_VIDEO_PAYLOAD["media"][0], "width": None, "height": 1080, "duration": 0}],
    }

    def extraction(route: Any, _request: Any) -> None:
        """Return a video with incomplete dimensions and zero duration."""
        fulfill_json(route, payload)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    metadata = page.locator(".media-metadata")
    expect(metadata).to_contain_text("長度")
    expect(metadata).to_contain_text("00:00")
    expect(metadata).not_to_contain_text("尺寸")
    expect(metadata).not_to_contain_text("作者")
    expect(metadata).not_to_contain_text("null")
    expect(metadata).not_to_contain_text("NaN")


def test_media_metadata_formats_hour_duration(page: Page, base_url: str) -> None:
    """Verify durations of at least one hour use the HH:MM:SS representation."""
    payload = {**X_VIDEO_PAYLOAD, "media": [{**X_VIDEO_PAYLOAD["media"][0], "duration": 3723}]}

    def extraction(route: Any, _request: Any) -> None:
        """Return one video with an hour-scale duration."""
        fulfill_json(route, payload)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    expect(page.locator(".media-metadata")).to_contain_text("01:02:03")


def test_copy_filename_reports_success_and_failure_without_disabling_download(
    page: Page, base_url: str
) -> None:
    """Verify Clipboard outcomes are bounded and never affect the download control."""

    def extraction(route: Any, _request: Any) -> None:
        """Return a deterministic filename for the copy interaction."""
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.add_init_script(
        """Object.defineProperty(navigator, 'clipboard', {
          value: { writeText: () => Promise.resolve() }, configurable: true
        });"""
    )
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    page.get_by_role("button", name="複製檔名").click()
    expect(page.get_by_role("button", name="已複製檔名")).to_be_visible()
    assert page.locator(".download-action").is_enabled()

    page.evaluate(
        """() => {
          navigator.clipboard.writeText = () => Promise.reject(new Error('clipboard denied'));
        }"""
    )
    page.get_by_role("button", name="已複製檔名").click()
    expect(page.get_by_role("button", name="無法複製檔名")).to_be_visible()
    assert page.locator(".download-action").is_enabled()


def test_generated_preview_renders_and_failed_preview_uses_local_fallback(
    page: Page, base_url: str
) -> None:
    """Verify generated preview responses render and failures remain local-only."""
    payload = {
        **SUCCESS_PAYLOAD,
        "media": [
            {
                **SUCCESS_PAYLOAD["media"][0],
                "preview_url": "/api/media/opaque-preview/preview",
            }
        ],
    }
    preview_calls: list[str] = []

    def extraction(route: Any, _request: Any) -> None:
        """Return one item with a token-bound preview URL."""
        fulfill_json(route, payload)

    def preview(route: Any, request: Any) -> None:
        """Return success once, then a real non-2xx preview response."""
        preview_calls.append(request.url)
        if len(preview_calls) > 1:
            route.fulfill(
                status=502,
                content_type="application/json",
                body=b'{"code":"upstream_media_invalid"}',
            )
            return
        body = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        route.fulfill(status=200, content_type="image/png", body=body)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-preview/preview**", preview)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")
    expect(page.locator(".media-visual img")).to_be_visible()

    page.locator(".media-visual img").evaluate("element => element.src = element.src + '?retry=1'")
    expect(page.locator(".fallback-tile")).to_contain_text("找不到預覽")
    assert len(preview_calls) == 2


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
    grid_box = page.locator(".media-grid").bounding_box()
    card_box = page.locator(".media-card").bounding_box()
    assert grid_box is not None
    assert card_box is not None
    assert abs(grid_box["width"] - card_box["width"]) <= 1
    assert extraction_calls == [{"url": "https://x.com/creator/status/1"}]


def test_analysis_mode_switch_shows_only_the_selected_input_panel(
    page: Page, base_url: str
) -> None:
    """Verify users can switch between preserved single and batch URL inputs."""
    page.goto(base_url)

    expect(page.locator("#single-mode-button")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#batch-input-panel")).to_be_hidden()
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.get_by_role("button", name="批次分析").click()
    expect(page.locator("#batch-mode-button")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#single-input-panel")).to_be_hidden()
    expect(page.locator("#batch-input-panel")).to_be_visible()
    page.fill("#batch-post-urls", "https://x.com/creator/status/2")
    page.get_by_role("button", name="單筆分析").click()
    expect(page.locator("#post-url")).to_have_value("https://x.com/creator/status/1")


def test_batch_button_submits_each_parsed_url(page: Page, base_url: str) -> None:
    """Verify batch analysis uses the existing extraction endpoint for each URL."""
    submitted: list[str] = []

    def extraction(route: Any, request: Any) -> None:
        """Record each sequential client extraction request."""
        submitted.append(json.loads(request.post_data or "{}")["url"])
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1")
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator("#status")).to_contain_text("準備就緒")
    assert submitted == ["https://x.com/creator/status/1"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (" \n\t\n", "請至少輸入 1 個 URL"),
        (
            "\n".join(f"https://x.com/creator/status/{index}" for index in range(1, 7)),
            "最多只能分析 5 個 URL",
        ),
    ],
)
def test_batch_parser_rejects_invalid_complete_input(
    page: Page, base_url: str, value: str, message: str
) -> None:
    """Verify empty and over-limit batches never submit an extraction request."""
    requests: list[str] = []

    def extraction(route: Any, request: Any) -> None:
        """Record unexpected client requests while returning a valid payload."""
        requests.append(request.url)
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", value)
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator("#status")).to_contain_text(message)
    assert requests == []


def test_batch_parser_trims_deduplicates_and_preserves_first_seen_order(
    page: Page, base_url: str
) -> None:
    """Verify a valid mixed-platform batch submits at most five unique URLs in source order."""
    submitted: list[str] = []
    urls = [
        "https://www.instagram.com/p/POST001/",
        "https://x.com/creator/status/2",
        "https://www.instagram.com/reel/REEL003/",
        "https://x.com/creator/status/4",
        "https://www.instagram.com/p/POST005/",
    ]

    def extraction(route: Any, request: Any) -> None:
        """Record each parser-produced URL and return a stable success payload."""
        submitted.append(json.loads(request.post_data or "{}")["url"])
        fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill(
        "#batch-post-urls",
        f"  {urls[0]}  \n\n{urls[1]}\n {urls[0]}\n{urls[2]}\n{urls[3]}\n{urls[4]} ",
    )
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator("#status")).to_contain_text("準備就緒")
    assert submitted == urls


def test_batch_queue_continues_after_a_mapped_item_error(page: Page, base_url: str) -> None:
    """Verify a failed batch item does not prevent the next parsed URL from being analyzed."""
    submitted: list[str] = []

    def extraction(route: Any, request: Any) -> None:
        """Return one bounded validation error between two successful responses."""
        submitted.append(json.loads(request.post_data or "{}")["url"])
        if len(submitted) == 2:
            fulfill_json(route, {"code": "unsupported_url"}, status=400)
        else:
            fulfill_json(route, SUCCESS_PAYLOAD)

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill(
        "#batch-post-urls",
        "https://x.com/creator/status/1\nhttps://invalid.example/post\nhttps://x.com/creator/status/3",
    )
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator("#status")).to_contain_text("準備就緒")
    assert submitted == [
        "https://x.com/creator/status/1",
        "https://invalid.example/post",
        "https://x.com/creator/status/3",
    ]


def test_batch_item_disables_competing_extraction_actions(page: Page, base_url: str) -> None:
    """Verify a running batch item prevents every other extraction entry point from submitting."""
    extraction_server = ExtractionServer([SUCCESS_PAYLOAD])

    def extraction(route: Any, request: Any) -> None:
        """Route API requests to the controllable delayed extraction server."""
        route_extraction_to_server(route, request, extraction_server)

    page.route("**/api/extractions", extraction)
    try:
        page.goto(base_url)
        page.get_by_role("button", name="批次分析").click()
        page.fill("#batch-post-urls", "https://x.com/creator/status/1")
        page.get_by_role("button", name="開始批次分析").click()
        wait_for_condition(page, lambda: len(extraction_server.requests) == 1)

        expect(page.locator("#analyze-button")).to_be_disabled()
        expect(page.locator("#analyze-batch-button")).to_be_disabled()
        page.get_by_role("button", name="單筆分析").click()
        page.fill("#post-url", "https://x.com/creator/status/2")
        assert page.locator("#analyze-button").is_disabled()
        assert len(extraction_server.requests) == 1
    finally:
        extraction_server.close()


def test_single_analysis_blocks_batch_until_its_request_settles(page: Page, base_url: str) -> None:
    """驗證單筆未完成時不能啟動批次，成功後會恢復有效控制項。"""
    extraction_server = ExtractionServer([SUCCESS_PAYLOAD])

    def extraction(route: Any, request: Any) -> None:
        """將單筆與批次請求都導向可延遲的 extraction server。"""
        route_extraction_to_server(route, request, extraction_server)

    page.route("**/api/extractions", extraction)
    try:
        page.goto(base_url)
        page.fill("#post-url", "https://x.com/creator/status/1")
        page.click("#analyze-button")
        wait_for_condition(page, lambda: len(extraction_server.requests) == 1)

        expect(page.locator("#analyze-batch-button")).to_be_disabled()
        page.get_by_role("button", name="批次分析").click()
        page.fill("#batch-post-urls", "https://x.com/creator/status/2")
        assert page.locator("#analyze-batch-button").is_disabled()
        assert len(extraction_server.requests) == 1

        extraction_server.release_first.set()
        expect(page.locator("#analyze-batch-button")).to_be_enabled()
        page.get_by_role("button", name="開始批次分析").click()
        wait_for_condition(page, lambda: len(extraction_server.requests) == 2)
    finally:
        extraction_server.close()


def test_group_recovery_blocks_other_starts_until_a_mapped_error(page: Page, base_url: str) -> None:
    """驗證群組 recovery 未完成時互斥，mapped error 後會釋放有效控制項。"""
    recovery_server = ExtractionServer([{"code": "extraction_timeout"}], [504])
    extraction_calls = 0

    def extraction(route: Any, request: Any) -> None:
        """首次回傳結果，第二次 recovery 則延遲並回傳 mapped error。"""
        nonlocal extraction_calls
        extraction_calls += 1
        if extraction_calls == 1:
            fulfill_json(route, SUCCESS_PAYLOAD)
            return
        route_extraction_to_server(route, request, recovery_server)

    def expired_download(route: Any, _request: Any) -> None:
        """以 token 過期回應顯示群組局部 recovery 控制項。"""
        fulfill_json(route, {"code": "token_expired"}, status=410)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", expired_download)
    try:
        page.goto(base_url)
        page.fill("#post-url", "https://x.com/creator/status/1")
        page.click("#analyze-button")
        page.get_by_role("button", name="下載", exact=True).click()
        page.get_by_role("button", name="重新分析此項").click()
        wait_for_condition(page, lambda: len(recovery_server.requests) == 1)

        expect(page.locator("#analyze-button")).to_be_disabled()
        expect(page.locator("#analyze-batch-button")).to_be_disabled()
        assert extraction_calls == 2

        recovery_server.release_first.set()
        expect(page.locator("#status")).to_contain_text("平台回應時間過長")
        expect(page.locator("#analyze-button")).to_be_enabled()
        expect(page.locator("#analyze-batch-button")).to_be_enabled()
    finally:
        recovery_server.close()


def test_stop_queue_allows_running_item_and_skips_pending_items(page: Page, base_url: str) -> None:
    """Verify stopping a batch waits for its running request but never starts pending URLs."""
    extraction_server = ExtractionServer([SUCCESS_PAYLOAD])

    def extraction(route: Any, request: Any) -> None:
        """Route batch requests to a server whose first response is held open."""
        route_extraction_to_server(route, request, extraction_server)

    page.route("**/api/extractions", extraction)
    try:
        page.goto(base_url)
        page.get_by_role("button", name="批次分析").click()
        page.fill(
            "#batch-post-urls",
            "https://x.com/creator/status/1\nhttps://x.com/creator/status/2",
        )
        page.get_by_role("button", name="開始批次分析").click()
        wait_for_condition(page, lambda: len(extraction_server.requests) == 1)
        page.get_by_role("button", name="停止批次分析").click()
        extraction_server.release_first.set()
        expect(page.locator("#analyze-button")).to_be_enabled()
        expect(page.locator("#analyze-batch-button")).to_be_enabled()

        assert len(extraction_server.requests) == 1
    finally:
        extraction_server.close()


def test_queue_progress_exposes_running_success_and_stopped_lifecycle(
    page: Page, base_url: str
) -> None:
    """驗證 queue 會公告執行中、成功與停止的獨立項目生命週期。"""
    extraction_server = ExtractionServer([SUCCESS_PAYLOAD])

    def extraction(route: Any, request: Any) -> None:
        """將第一筆 queue request 保持未完成以觀察停止前後狀態。"""
        route_extraction_to_server(route, request, extraction_server)

    page.route("**/api/extractions", extraction)
    try:
        page.goto(base_url)
        page.get_by_role("button", name="批次分析").click()
        page.fill(
            "#batch-post-urls",
            "https://x.com/creator/status/1\nhttps://x.com/creator/status/2",
        )
        page.get_by_role("button", name="開始批次分析").click()
        wait_for_condition(page, lambda: len(extraction_server.requests) == 1)

        expect(page.locator("#queue-progress")).to_contain_text("0 / 2")
        expect(page.locator(".queue-item").nth(0)).to_contain_text("執行中")
        expect(page.locator(".queue-item").nth(1)).to_contain_text("等待中")
        page.get_by_role("button", name="停止批次分析").click()
        expect(page.locator(".queue-item").nth(1)).to_contain_text("已停止")
        expect(page.locator("#queue-progress")).to_contain_text("1 項已停止")

        extraction_server.release_first.set()
        expect(page.locator(".queue-item").nth(0)).to_contain_text("成功")
        expect(page.locator("#queue-progress")).to_contain_text("1 / 2")
        assert extraction_server.requests == ["https://x.com/creator/status/1"]
    finally:
        extraction_server.close()


def test_batch_results_remain_as_independent_groups(page: Page, base_url: str) -> None:
    """Verify successful batch items remain visible as separate result groups."""
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """Return a distinct payload for each sequential batch request."""
        nonlocal calls
        calls += 1
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": f"Batch {calls}"})

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator(".result-group")).to_have_count(2)


def test_batch_result_groups_use_a_keyboard_navigable_scroll_snap_track(
    page: Page, base_url: str
) -> None:
    """驗證批次結果在窄螢幕使用可鍵盤操作的水平 scroll-snap 軌道。"""
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳三個結果以建立可滑動的群組軌道。"""
        nonlocal calls
        calls += 1
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": f"Batch {calls}"})

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": 320, "height": 800})
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill(
        "#batch-post-urls",
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2\nhttps://x.com/creator/status/3",
    )
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(3)
    expect(page.locator("#result-groups")).to_have_css("scroll-snap-type", "x mandatory")
    expect(page.get_by_role("status", name="結果群組位置")).to_have_text("第 1/3 組")
    expect(page.get_by_role("button", name="上一組結果")).to_be_disabled()
    page.get_by_role("button", name="下一組結果").press("Enter")
    expect(page.get_by_role("status", name="結果群組位置")).to_have_text("第 2/3 組")
    expect(page.get_by_role("button", name="上一組結果")).to_be_enabled()
    page.locator("#result-groups").evaluate("element => element.scrollTo({ left: element.scrollWidth })")
    expect(page.get_by_role("status", name="結果群組位置")).to_have_text("第 3/3 組")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_desktop_result_group_navigation_buttons_are_visible_and_operable(
    page: Page, base_url: str
) -> None:
    """驗證桌面版可見的前後群組按鈕能切換位置摘要。"""
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳三個結果群組供桌面導覽操作。"""
        nonlocal calls
        calls += 1
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": f"Desktop {calls}"})

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill(
        "#batch-post-urls",
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2\nhttps://x.com/creator/status/3",
    )
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.get_by_role("button", name="上一組結果")).to_be_visible()
    expect(page.get_by_role("button", name="下一組結果")).to_be_visible()
    page.get_by_role("button", name="下一組結果").click()
    expect(page.get_by_role("status", name="結果群組位置")).to_have_text("第 2/3 組")
    page.get_by_role("button", name="上一組結果").click()
    expect(page.get_by_role("status", name="結果群組位置")).to_have_text("第 1/3 組")


def test_touch_scroll_updates_result_group_position_summary(touch_page: Page, base_url: str) -> None:
    """驗證觸控 context 的水平滑動會更新目前結果群組摘要。"""
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳三個結果群組供觸控滑動測試。"""
        nonlocal calls
        calls += 1
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": f"Touch {calls}"})

    touch_page.route("**/api/extractions", extraction)
    touch_page.goto(base_url)
    assert touch_page.evaluate("matchMedia('(pointer: coarse)').matches")
    touch_page.get_by_role("button", name="批次分析").click()
    touch_page.fill(
        "#batch-post-urls",
        "https://x.com/creator/status/1\nhttps://x.com/creator/status/2\nhttps://x.com/creator/status/3",
    )
    touch_page.get_by_role("button", name="開始批次分析").click()

    expect(touch_page.get_by_role("status", name="結果群組位置")).to_have_text("第 1/3 組")
    touch_page.locator("#result-groups").dispatch_event(
        "pointerdown", {"pointerType": "touch", "pointerId": 1, "clientX": 300}
    )
    touch_page.locator("#result-groups").evaluate(
        "element => element.scrollBy({ left: element.clientWidth })"
    )
    touch_page.locator("#result-groups").dispatch_event(
        "pointermove", {"pointerType": "touch", "pointerId": 1, "clientX": 40}
    )
    touch_page.locator("#result-groups").dispatch_event(
        "pointerup", {"pointerType": "touch", "pointerId": 1, "clientX": 40}
    )
    expect(touch_page.get_by_role("status", name="結果群組位置")).to_have_text("第 2/3 組")


def test_batch_result_group_selection_is_isolated(page: Page, base_url: str) -> None:
    """驗證批次結果群組的選取控制項不會影響其他群組。"""
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳兩個可辨識的成功結果群組。"""
        nonlocal calls
        calls += 1
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": f"Batch {calls}"})

    page.route("**/api/extractions", extraction)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.nth(0).get_by_role("button", name="全選", exact=True).click()

    expect(groups.nth(0).locator(".selection-summary")).to_contain_text("1 / 1")
    expect(groups.nth(1).locator(".selection-summary")).to_contain_text("0 / 1")
    expect(groups.nth(0).get_by_role("button", name="下載選取項目")).to_be_enabled()
    expect(groups.nth(1).get_by_role("button", name="下載選取項目")).to_be_disabled()


def test_batch_result_groups_share_the_global_preview_coordinator(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """驗證不同批次結果群組的 opaque 預覽仍維持全域單一併發。"""
    payloads = [
        build_preview_payload(["/api/media/first-group/preview"], post_number="1"),
        build_preview_payload(["/api/media/second-group/preview"], post_number="2"),
    ]
    calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依批次順序回傳各自帶有 opaque 預覽的結果。"""
        nonlocal calls
        fulfill_json(route, payloads[calls])
        calls += 1

    def preview(route: Any, request: Any) -> None:
        """將 opaque 預覽改導向可觀測併發的本機伺服器。"""
        route_preview_to_server(route, request, preview_server)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator(".result-group")).to_have_count(2)
    wait_for_loaded_previews(page, 2)
    assert preview_server.max_active == 1
    assert preview_server.started_tokens == ["first-group", "second-group"]


def test_reanalyzing_one_batch_group_preserves_another_group(page: Page, base_url: str) -> None:
    """驗證重新分析一個批次群組時，其他群組的內容與選取狀態保持不變。"""
    initial_payloads = [
        {**SUCCESS_PAYLOAD, "description": "Group A", "post_url": "https://x.com/creator/status/1"},
        {**SUCCESS_PAYLOAD, "description": "Group B", "post_url": "https://x.com/creator/status/2"},
    ]
    refreshed_payload = {
        **SUCCESS_PAYLOAD,
        "description": "Refreshed A",
        "post_url": "https://x.com/creator/status/1",
    }
    extraction_calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳兩個初始群組與第一群組的新 payload。"""
        nonlocal extraction_calls
        payload = initial_payloads[extraction_calls] if extraction_calls < 2 else refreshed_payload
        extraction_calls += 1
        fulfill_json(route, payload)

    def expired_download(route: Any, _request: Any) -> None:
        """以安全的過期 token 回應觸發群組局部重新分析。"""
        fulfill_json(route, {"code": "token_expired"}, status=410)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", expired_download)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.nth(1).locator(".media-selection").check()
    groups.nth(0).get_by_role("button", name="下載", exact=True).click()
    groups.nth(0).get_by_role("button", name="重新分析此項").click()

    expect(groups.nth(0).locator(".post-description")).to_have_text("Refreshed A")
    expect(groups.nth(0).locator(".selection-summary")).to_contain_text("0 / 1")
    expect(groups.nth(1).locator(".post-description")).to_have_text("Group B")
    assert groups.nth(1).locator(".media-selection").is_checked()
    assert extraction_calls == 3


@pytest.mark.parametrize("token_error", ["token_expired", "token_not_found"])
def test_group_recovery_is_hidden_until_a_token_failure(
    page: Page, base_url: str, token_error: str
) -> None:
    """驗證群組只在下載參照失效後顯示局部重新分析操作。"""

    def extraction(route: Any, _request: Any) -> None:
        """回傳一個可下載的結果群組。"""
        fulfill_json(route, SUCCESS_PAYLOAD)

    def failed_download(route: Any, _request: Any) -> None:
        """回傳指定的安全 token 失效錯誤。"""
        fulfill_json(route, {"code": token_error}, status=410)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", failed_download)
    page.goto(base_url)
    page.fill("#post-url", "https://x.com/creator/status/1")
    page.click("#analyze-button")

    group = page.locator(".result-group")
    expect(group.locator(".group-recovery .re-analyze")).to_have_count(0)
    group.get_by_role("button", name="下載", exact=True).click()
    expect(group.get_by_role("button", name="重新分析此項")).to_be_visible()


def test_group_recovery_replaces_only_target_clears_its_selection_and_does_not_rerun_batch(
    page: Page, base_url: str
) -> None:
    """驗證成功 recovery 只更新目標群組、清空其選取且不重送批次項目。"""
    initial_payloads = [
        {**SUCCESS_PAYLOAD, "description": "Group A", "post_url": "https://x.com/creator/status/1"},
        {**SUCCESS_PAYLOAD, "description": "Group B", "post_url": "https://x.com/creator/status/2"},
    ]
    refreshed_payload = {
        **SUCCESS_PAYLOAD,
        "description": "Refreshed B",
        "post_url": "https://x.com/creator/status/2",
    }
    submitted: list[str] = []

    def extraction(route: Any, request: Any) -> None:
        """依請求順序回傳兩個 batch 結果與第二組的更新結果。"""
        submitted.append(json.loads(request.post_data or "{}")["url"])
        payloads = [*initial_payloads, refreshed_payload]
        fulfill_json(route, payloads[len(submitted) - 1])

    def expired_download(route: Any, _request: Any) -> None:
        """讓第二個群組的下載預檢要求重新分析。"""
        fulfill_json(route, {"code": "token_expired"}, status=410)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", expired_download)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.nth(0).locator(".media-selection").check()
    groups.nth(1).locator(".media-selection").check()
    groups.nth(1).get_by_role("button", name="下載", exact=True).click()
    groups.nth(1).get_by_role("button", name="重新分析此項").click()

    expect(groups.nth(1).locator(".post-description")).to_have_text("Refreshed B")
    expect(groups.nth(1).locator(".selection-summary")).to_contain_text("0 / 1")
    assert not groups.nth(1).locator(".media-selection").is_checked()
    expect(groups.nth(0).locator(".post-description")).to_have_text("Group A")
    assert groups.nth(0).locator(".media-selection").is_checked()
    assert submitted == [
        "https://x.com/creator/status/1",
        "https://x.com/creator/status/2",
        "https://x.com/creator/status/2",
    ]


def test_failed_group_recovery_keeps_other_batch_groups_available(
    page: Page, base_url: str
) -> None:
    """驗證局部重新分析失敗時，其他群組仍可保留並繼續操作。"""
    initial_payloads = [
        {**SUCCESS_PAYLOAD, "description": "Group A", "post_url": "https://x.com/creator/status/1"},
        {**SUCCESS_PAYLOAD, "description": "Group B", "post_url": "https://x.com/creator/status/2"},
    ]
    requests = 0

    def extraction(route: Any, _request: Any) -> None:
        """先回傳兩個 batch 結果，再讓目標 recovery 回傳安全錯誤。"""
        nonlocal requests
        requests += 1
        if requests <= 2:
            fulfill_json(route, initial_payloads[requests - 1])
        else:
            fulfill_json(route, {"code": "extraction_timeout"}, status=504)

    def missing_download(route: Any, _request: Any) -> None:
        """以遺失 token 觸發第一組的局部 recovery。"""
        fulfill_json(route, {"code": "token_not_found"}, status=404)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", missing_download)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    groups.nth(1).locator(".media-selection").check()
    groups.nth(0).get_by_role("button", name="下載", exact=True).click()
    groups.nth(0).get_by_role("button", name="重新分析此項").click()

    expect(page.locator("#status")).to_contain_text("平台回應時間過長")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".post-description")).to_have_text("Group A")
    expect(groups.nth(1).locator(".post-description")).to_have_text("Group B")
    assert groups.nth(1).locator(".media-selection").is_checked()
    assert groups.nth(1).get_by_role("button", name="下載選取項目").is_enabled()


def test_failed_group_recovery_shows_a_safe_error_in_only_the_target_group(
    page: Page, base_url: str
) -> None:
    """驗證 recovery 失敗的安全錯誤只會保留在目標群組的 live status。"""
    initial_payloads = [
        {**SUCCESS_PAYLOAD, "description": "Group A", "post_url": "https://x.com/creator/status/1"},
        {**SUCCESS_PAYLOAD, "description": "Group B", "post_url": "https://x.com/creator/status/2"},
    ]
    requests = 0

    def extraction(route: Any, _request: Any) -> None:
        """先建立兩個群組，再讓第一組 recovery 回傳安全逾時錯誤。"""
        nonlocal requests
        requests += 1
        if requests <= 2:
            fulfill_json(route, initial_payloads[requests - 1])
        else:
            fulfill_json(
                route,
                {"code": "extraction_timeout", "message": "hidden raw detail"},
                status=504,
            )

    def missing_download(route: Any, _request: Any) -> None:
        """以遺失下載參照觸發第一個結果群組的局部 recovery。"""
        fulfill_json(route, {"code": "token_not_found"}, status=404)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/opaque-download/download", missing_download)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".source-link")).to_have_attribute(
        "href", "https://x.com/creator/status/1"
    )
    expect(groups.nth(0).locator(".results-summary")).to_contain_text("1 個媒體項目")
    expect(groups.nth(0).locator(".warning")).to_contain_text("1 個媒體項目無法")
    groups.nth(0).get_by_role("button", name="下載", exact=True).click()
    groups.nth(0).get_by_role("button", name="重新分析此項").click()

    expect(groups.nth(0).locator(".group-error")).to_contain_text("平台回應時間過長")
    expect(groups.nth(0).locator(".group-error")).not_to_contain_text("hidden raw detail")
    expect(groups.nth(1).locator(".group-error")).to_be_hidden()
    expect(groups.nth(1).locator(".group-error")).to_have_text("")
    expect(groups.nth(0).locator(".group-status[data-state]")).to_have_attribute(
        "aria-live", "polite"
    )


@pytest.mark.parametrize("viewport_width", [320, 375, 768])
def test_batch_groups_have_no_horizontal_overflow_at_supported_widths(
    page: Page, base_url: str, viewport_width: int
) -> None:
    """驗證批次 queue 與群組結果在指定窄螢幕寬度都不會造成水平捲動。"""

    def extraction(route: Any, _request: Any) -> None:
        """回傳帶有長描述的安全資料以覆蓋可換行的群組內容。"""
        fulfill_json(route, {**SUCCESS_PAYLOAD, "description": "長描述 " * 80})

    page.route("**/api/extractions", extraction)
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    expect(page.locator(".result-group")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_stale_preview_after_group_replacement_cannot_mutate_other_groups(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """驗證被替換群組的舊 preview callback 不會改動新群組或其他群組。"""
    old_preview = "old-group-preview"
    preview_server.blocked_tokens.add(old_preview)
    initial_payloads = [
        build_preview_payload([f"/api/media/{old_preview}/preview"], post_number="1"),
        build_preview_payload(["/placeholder.svg"], post_number="2"),
    ]
    initial_payloads[0]["description"] = "Old Group A"
    initial_payloads[1] = build_preview_payload(
        ["/api/media/group-b-preview/preview"], post_number="2"
    )
    initial_payloads[1]["description"] = "Group B"
    refreshed_payload = build_preview_payload(["/placeholder.svg"], post_number="1")
    refreshed_payload["description"] = "Refreshed Group A"
    extraction_calls = 0

    def extraction(route: Any, _request: Any) -> None:
        """依序回傳舊 A、穩定 B 與替換後 A 的結果。"""
        nonlocal extraction_calls
        payloads = [*initial_payloads, refreshed_payload]
        fulfill_json(route, payloads[extraction_calls])
        extraction_calls += 1

    def preview(route: Any, request: Any) -> None:
        """將舊 A 的 opaque preview 導向可控的延遲回應。"""
        route_preview_to_server(route, request, preview_server)

    def expired_download(route: Any, _request: Any) -> None:
        """以過期 token 建立群組局部替換操作。"""
        fulfill_json(route, {"code": "token_expired"}, status=410)

    page.route("**/api/extractions", extraction)
    page.route("**/api/media/**/preview**", preview)
    page.route("**/api/media/download-1-1/download", expired_download)
    page.goto(base_url)
    page.get_by_role("button", name="批次分析").click()
    page.fill("#batch-post-urls", "https://x.com/creator/status/1\nhttps://x.com/creator/status/2")
    page.get_by_role("button", name="開始批次分析").click()

    groups = page.locator(".result-group")
    expect(groups).to_have_count(2)
    wait_for_condition(page, lambda: preview_server.attempts_for(old_preview) == 1)
    groups.nth(0).get_by_role("button", name="下載", exact=True).click()
    groups.nth(0).get_by_role("button", name="重新分析此項").click()
    expect(groups.nth(0).locator(".post-description")).to_have_text("Refreshed Group A")
    wait_for_condition(page, lambda: preview_server.attempts_for("group-b-preview") == 1)
    preview_server.release.set()
    page.wait_for_timeout(150)

    assert preview_server.attempts_for(old_preview) == 1
    assert preview_server.attempts_for("group-b-preview") == 1
    expect(groups.nth(1).locator(".post-description")).to_have_text("Group B")
    expect(groups.nth(1).locator(".media-visual img")).to_have_count(1)


def test_reduced_motion_and_external_asset_boundaries(page: Page, base_url: str) -> None:
    """Verify reduced motion and same-origin static asset constraints."""
    page.emulate_media(reduced_motion="reduce")
    page.goto(base_url)

    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.locator("script[src^='http']").count() == 0
    assert page.locator("link[href^='http']").count() == 0
