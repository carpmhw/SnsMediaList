"""Tests for bounded media response streaming and response headers."""

import asyncio

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.media_client import (
    MediaResponse,
    build_download_headers,
    build_forward_response_headers,
    iter_validated_body,
)


class Reader:
    """Provide deterministic response body reads."""

    def __init__(self, body: bytes) -> None:
        """Store body bytes for chunked reads."""
        self.body = body

    async def read(self, size: int) -> bytes:
        """Return the next body chunk."""
        data, self.body = self.body[:size], self.body[size:]
        return data

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return an empty trailer for unused chunked tests."""
        return b"\r\n"

    async def readexactly(self, size: int) -> bytes:
        """Return exactly the requested bytes."""
        data, self.body = self.body[:size], self.body[size:]
        return data


class Writer:
    """Provide no-op connection cleanup for response tests."""

    def close(self) -> None:
        """Close the fake connection."""

    async def wait_closed(self) -> None:
        """Complete fake connection cleanup."""


class SlowReader(Reader):
    """Provide a body reader that exceeds the configured read timeout."""

    async def read(self, size: int) -> bytes:
        """Sleep beyond the response deadline before reading."""
        await asyncio.sleep(1)
        return await super().read(size)


def make_response(
    body: bytes,
    *,
    content_type: str,
    status_code: int = 200,
    max_bytes: int = 100,
    read_timeout: float = 1.0,
    reader: Reader | None = None,
) -> MediaResponse:
    """Construct a local upstream response for streaming tests."""
    return MediaResponse(
        status_code,
        {
            "content-type": content_type,
            "content-length": str(len(body)),
            "set-cookie": "session=secret",
        },
        reader or Reader(body),
        Writer(),
        max_bytes=max_bytes,
        read_timeout=read_timeout,
    )


async def collect(response: MediaResponse, *, preview: bool = False) -> bytes:
    """Collect a validated body and always close the upstream response."""
    try:
        chunks = [chunk async for chunk in iter_validated_body(response, preview=preview)]
        return b"".join(chunks)
    finally:
        await response.close()


@pytest.mark.asyncio
async def test_image_download_streams_body_and_strips_upstream_cookie() -> None:
    """Verify a valid image is streamed and response cookies are not forwarded."""
    response = make_response(b"\xff\xd8\xffimage", content_type="image/jpeg")

    body = await collect(response)

    assert body == b"\xff\xd8\xffimage"
    headers = build_forward_response_headers(response, filename="image.jpg", preview=False)
    assert "Set-Cookie" not in headers
    assert headers["Content-Disposition"].startswith("attachment;")


@pytest.mark.asyncio
async def test_preview_buffers_and_validates_signature_before_yielding() -> None:
    """Verify raster preview bytes are checked before being sent to the browser."""
    response = make_response(b"\x89PNG\r\n\x1a\nimage", content_type="image/png")

    body = await collect(response, preview=True)

    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert build_forward_response_headers(response, filename="poster.png", preview=True)[
        "Content-Disposition"
    ].startswith("inline;")


@pytest.mark.asyncio
async def test_preview_rejects_html_disguised_as_image() -> None:
    """Verify mislabeled HTML never streams from the application origin."""
    response = make_response(b"<html>not-an-image</html>", content_type="image/jpeg")

    with pytest.raises(AppError) as exc_info:
        await collect(response, preview=True)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_non_success_or_wrong_media_class_is_rejected() -> None:
    """Verify upstream errors and incompatible MIME types are not forwarded."""
    response = make_response(b"error", content_type="text/html", status_code=500)

    with pytest.raises(AppError) as exc_info:
        await collect(response)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_response_byte_limit_terminates_stream() -> None:
    """Verify oversized upstream bodies are stopped at the configured limit."""
    response = make_response(b"\x00" * 101, content_type="video/mp4", max_bytes=100)

    with pytest.raises(AppError) as exc_info:
        await collect(response)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_upstream_read_timeout_terminates_stream() -> None:
    """Verify a stalled upstream body is terminated."""
    response = make_response(
        b"body",
        content_type="image/jpeg",
        read_timeout=0.01,
        reader=SlowReader(b"body"),
    )

    with pytest.raises(AppError) as exc_info:
        await collect(response)

    assert exc_info.value.code == "upstream_media_invalid"


def test_download_filename_is_sanitized() -> None:
    """Verify download response headers cannot inject a filename."""
    headers = build_download_headers("../evil\r\n.txt")

    assert ".." not in headers["Content-Disposition"]
    assert "\r" not in headers["Content-Disposition"]
