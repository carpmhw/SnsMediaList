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

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        """Start a threaded extraction server using payloads in request order."""
        self._lock = Lock()
        self.payloads = payloads
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

    def response_for(self, url: str) -> dict[str, Any]:
        """Record one extraction URL and return its ordered response payload."""
        with self._lock:
            index = len(self.requests)
            self.requests.append(url)
        if index == 0:
            self.first_started.set()
            self.release_first.wait(timeout=5)
        return self.payloads[min(index, len(self.payloads) - 1)]

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
        response_payload = self.state.response_for(str(payload.get("url", "")))
        body = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
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
    expect(page.locator(".media-index")).to_have_text("項目 01")


def test_late_extraction_response_cannot_replace_a_newer_result(
    page: Page, base_url: str, preview_server: PreviewServer
) -> None:
    """Verify an older overlapping extraction response is ignored after a newer one."""
    old_payload = build_preview_payload(
        [
            "/api/media/old-one/preview",
            "/api/media/old-two/preview",
        ],
        post_number="old",
    )
    new_payload = build_preview_payload(["/api/media/new-one/preview"], post_number="new")
    extraction_server = ExtractionServer([old_payload, new_payload])

    def extraction(route: Any, request: Any) -> None:
        """Rewrite overlapping extraction requests to the delayed local server."""
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
    wait_for_condition(page, lambda: len(extraction_server.requests) == 2)
    expect(page.locator("#results-summary")).to_contain_text("1 個媒體項目已準備就緒")
    wait_for_loaded_previews(page, 1)

    extraction_server.release_first.set()
    page.wait_for_timeout(250)

    assert extraction_server.requests == [
        "https://x.com/creator/status/old",
        "https://x.com/creator/status/new",
    ]
    expect(page.locator("#source-link")).to_have_attribute(
        "href", "https://x.com/creator/status/new"
    )
    assert preview_server.attempts_for("old-one") == 0


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
    expect(page.locator(".media-title")).to_have_text("圖片")
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


def test_reduced_motion_and_external_asset_boundaries(page: Page, base_url: str) -> None:
    """Verify reduced motion and same-origin static asset constraints."""
    page.emulate_media(reduced_motion="reduce")
    page.goto(base_url)

    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.locator("script[src^='http']").count() == 0
    assert page.locator("link[href^='http']").count() == 0
