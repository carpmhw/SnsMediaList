"""Tests for token-bound preview and attachment routes."""

from typing import Any

from fastapi.testclient import TestClient

from sns_media_list.app import create_app
from sns_media_list.config import Settings
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


class FakeMediaClient:
    """Return an in-memory JPEG response for every authorized token."""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Return a valid image response without contacting a CDN."""
        del headers
        body = b"\xff\xd8\xff\xe0fake-image"
        return MediaResponse(
            200,
            {"content-type": "image/jpeg", "content-length": str(len(body))},
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
