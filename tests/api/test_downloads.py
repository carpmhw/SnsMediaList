"""Tests for token-bound preview and attachment routes."""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sns_media_list.api.limits import RequestLimiter
from sns_media_list.api.routes import _close_response_with_timeout, _stream_media
from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.models import PrivateMediaRecord
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.security.tokens import TokenStore
from sns_media_list.services.extraction_service import ExtractionService


class FakeExtractor:
    """Return one image with a preview token."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return deterministic media metadata."""
        return [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "image",
                "url": "https://pbs.twimg.com/media/1.jpg?name=orig",
                "preview_url": "https://pbs.twimg.com/media/1.jpg?name=small",
                "extension": "jpg",
                "width": 100,
                "height": 100,
                "progressive": True,
            }
        ]


class GeneratedExtractor:
    """Return one video without a platform poster for generated-preview tests."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return deterministic media metadata without preview metadata."""
        return [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "video",
                "url": "https://video.twimg.com/1.mp4",
                "extension": "mp4",
                "width": 100,
                "height": 100,
                "progressive": True,
            }
        ]


class FakeThumbnailGenerator:
    """Return one bounded JPEG while counting generation calls."""

    def __init__(self) -> None:
        """Initialize the generation counter."""
        self.calls = 0

    async def generate(self, response: MediaResponse) -> bytes:
        """Return a deterministic JPEG and close the supplied response."""
        self.calls += 1
        await response.close()
        return b"\xff\xd8\xff\xe0generated\xff\xd9"


class FailingThumbnailGenerator:
    """Return one deterministic generation failure for negative-cache tests."""

    def __init__(self) -> None:
        """Initialize the failure counter."""
        self.calls = 0

    async def generate(self, response: MediaResponse) -> bytes:
        """Raise a safe deterministic error after closing the source."""
        self.calls += 1
        await response.close()
        error = AppError("upstream_media_invalid", "safe thumbnail failure")
        error.deterministic = True
        raise error


class BlockingThumbnailGenerator(FakeThumbnailGenerator):
    """Hold one generated preview open so a different token reaches saturation."""

    timeout_seconds = 1.0

    def __init__(self) -> None:
        """Initialize synchronization events for the concurrent endpoint test."""
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, response: MediaResponse) -> bytes:
        """Wait for the test to issue a second generated-preview request."""
        self.calls += 1
        self.started.set()
        await self.release.wait()
        await response.close()
        return b"\xff\xd8\xff\xe0generated\xff\xd9"


class TimedThumbnailGenerator(FakeThumbnailGenerator):
    """Expose a short generation deadline for fetch-timeout tests."""

    timeout_seconds = 0.01


class SlowMediaClient:
    """Delay source response headers beyond the generated preview deadline."""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Sleep until the route-level generation timeout cancels the fetch."""
        await asyncio.sleep(1)
        raise AssertionError("fetch should have been cancelled")


class Reader:
    """Provide a fixed image body to the fake media client."""

    def __init__(self, body: bytes) -> None:
        """Store the response body."""
        self.body = body

    async def read(self, size: int) -> bytes:
        """Return the next body chunk."""
        data, self.body = self.body[:size], self.body[size:]
        return data

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return an empty trailer."""
        return b"\r\n"

    async def readexactly(self, size: int) -> bytes:
        """Return the requested bytes."""
        data, self.body = self.body[:size], self.body[size:]
        return data


class Writer:
    """Provide no-op cleanup for fake upstream responses."""

    def close(self) -> None:
        """Close the fake writer."""

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


class FailingCloseResponse(MediaResponse):
    """Raise during upstream cleanup after a valid response body was read."""

    async def close(self) -> None:
        """Simulate a transport cleanup failure."""
        raise RuntimeError("cleanup failed")


class SlowCloseResponse(MediaResponse):
    """Delay response cleanup beyond a generated-preview deadline."""

    async def close(self) -> None:
        """Wait until the bounded cleanup cancels this close operation."""
        await asyncio.sleep(1)


class FailingCloseMediaClient:
    """Return one response whose cleanup fails after streaming."""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Return a valid image response with a failing close operation."""
        body = b"\xff\xd8\xff\xe0fake-image"
        return FailingCloseResponse(
            200,
            {"content-type": "image/jpeg", "content-length": str(len(body))},
            Reader(body),
            Writer(),
            max_bytes=100,
        )


class FakeMediaClient:
    """Return an in-memory JPEG response for every authorized token."""

    def __init__(self, *, status_code: int = 200, content_type: str = "image/jpeg") -> None:
        """Initialize captured upstream request headers for boundary assertions."""
        self.last_headers: dict[str, str] = {}
        self.status_code = status_code
        self.content_type = content_type

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Return a valid image response without contacting a CDN."""
        self.last_headers = dict(headers)
        body = b"\xff\xd8\xff\xe0fake-image"
        return MediaResponse(
            self.status_code,
            {"content-type": self.content_type, "content-length": str(len(body))},
            Reader(body),
            Writer(),
            max_bytes=100,
        )


def make_client() -> TestClient:
    """Build an API client with fake extractor and media transport."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=FakeExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    return TestClient(create_app(extraction_service=service, media_client=FakeMediaClient()))


def test_download_and_preview_use_bound_tokens() -> None:
    """Verify individual download and preview endpoints stream safely."""
    client = make_client()
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    media = extraction["media"][0]

    download = client.get(media["download_url"])
    preview = client.get(media["preview_url"])

    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_stream_cleanup_releases_download_lease_when_close_fails() -> None:
    """Verify response cleanup cannot strand a download limiter reservation."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    lease = limiter.acquire_download("test-client")
    record = PrivateMediaRecord(
        token="download-token",
        purpose="download",
        source_url="https://pbs.twimg.com/media/1.jpg?name=orig",
        media_class="image",
        filename="image.jpg",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )

    response = await _stream_media(
        record,
        FailingCloseMediaClient(),
        preview=False,
        lease=lease,
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        _ = [chunk async for chunk in response.body_iterator]

    assert lease.released is True


@pytest.mark.asyncio
async def test_stream_validation_cleanup_releases_download_lease_when_close_fails() -> None:
    """Verify prevalidation cleanup cannot strand a lease when the body class is invalid."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    lease = limiter.acquire_download("test-client")
    record = PrivateMediaRecord(
        token="download-token",
        purpose="download",
        source_url="https://pbs.twimg.com/media/1.jpg?name=orig",
        media_class="video",
        filename="video.mp4",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await _stream_media(
            record,
            FailingCloseMediaClient(),
            preview=False,
            lease=lease,
        )

    assert lease.released is True


@pytest.mark.asyncio
async def test_generated_response_cleanup_is_deadline_bounded() -> None:
    """Verify generated-preview cleanup cannot occupy a slot beyond its deadline."""
    body = b"\xff\xd8\xff\xe0fake-image"
    response = SlowCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(body))},
        Reader(body),
        Writer(),
        max_bytes=100,
    )

    await asyncio.wait_for(_close_response_with_timeout(response, timeout=0.01), timeout=0.1)


def test_generated_preview_is_created_lazily_and_cached() -> None:
    """Verify generated previews use the endpoint, safe headers, and one generation."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(content_type="video/mp4")
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )

    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    preview_url = extraction["media"][0]["preview_url"]

    first = client.get(preview_url)
    second = client.get(preview_url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["content-disposition"].startswith("inline;")
    assert first.headers["x-content-type-options"] == "nosniff"
    assert generator.calls == 1


def test_generated_preview_deterministic_failure_is_cached() -> None:
    """Verify repeated deterministic generation failures do not repeat FFmpeg work."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    generator = FailingThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=FakeMediaClient(content_type="video/mp4"),
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    first = client.get(extraction["media"][0]["preview_url"])
    second = client.get(extraction["media"][0]["preview_url"])

    assert first.status_code == 502
    assert second.status_code == 502
    assert generator.calls == 1


@pytest.mark.asyncio
async def test_generated_preview_saturation_returns_immediate_rate_limit() -> None:
    """Verify a different generated token is rejected while the only slot is active."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    generator = BlockingThumbnailGenerator()
    app = create_app(
        extraction_service=service,
        media_client=FakeMediaClient(content_type="video/mp4"),
        thumbnail_generator=generator,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first_extraction = (
            await client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})
        ).json()
        second_extraction = (
            await client.post("/api/extractions", json={"url": "https://x.com/creator/status/2"})
        ).json()
        first_request = asyncio.create_task(client.get(first_extraction["media"][0]["preview_url"]))
        await generator.started.wait()

        saturated = await client.get(second_extraction["media"][0]["preview_url"])
        generator.release.set()
        first_response = await first_request

    assert saturated.status_code == 429
    assert saturated.headers["Retry-After"] == "1"
    assert saturated.json()["code"] == "local_rate_limited"
    assert first_response.status_code == 200
    assert generator.calls == 1


def test_generated_preview_rejects_upstream_status_before_generation() -> None:
    """Verify an error response cannot become a thumbnail even if its body looks valid."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(status_code=404)
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502
    assert generator.calls == 0


def test_generated_preview_rejects_mismatched_expected_media_class() -> None:
    """Verify a video token cannot use an image upstream response as its source."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(content_type="image/jpeg")
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502
    assert generator.calls == 0


def test_generated_preview_deadline_covers_upstream_fetch() -> None:
    """Verify the generation deadline includes upstream connection and headers."""
    settings = Settings()
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=SlowMediaClient(),
            thumbnail_generator=TimedThumbnailGenerator(),
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502


def test_media_proxy_sends_only_fixed_headers_without_platform_cookies() -> None:
    """Verify downstream media requests never inherit platform authentication."""
    settings = Settings()
    media_client = FakeMediaClient()
    service = ExtractionService(
        settings,
        extractor=FakeExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(create_app(extraction_service=service, media_client=media_client))
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["download_url"])

    assert response.status_code == 200
    assert media_client.last_headers == {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": "https://x.com/",
    }


def test_preview_token_cannot_be_used_for_download() -> None:
    """Verify token purpose mismatch never contacts the media client."""
    client = make_client()
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    preview_url = extraction["media"][0]["preview_url"]
    token = preview_url.split("/")[-2]

    response = client.get(f"/api/media/{token}/download")

    assert response.status_code == 404
    assert response.json()["code"] == "token_not_found"


def test_unknown_token_returns_not_found() -> None:
    """Verify random tokens have a stable missing-token response."""
    client = make_client()

    response = client.get("/api/media/random-token/download")

    assert response.status_code == 404
    assert response.json()["code"] == "token_not_found"
