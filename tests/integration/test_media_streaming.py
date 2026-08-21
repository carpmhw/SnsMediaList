"""Tests for bounded media response streaming and response headers."""

import asyncio
import ssl

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


class ResettingWriter(Writer):
    """Simulate an upstream peer reset during connection cleanup."""

    async def wait_closed(self) -> None:
        """Raise the reset produced when the upstream peer closes abruptly."""
        raise ConnectionResetError


class TlsClosingWriter(Writer):
    """Simulate a TLS shutdown error after the upstream body was consumed."""

    async def wait_closed(self) -> None:
        """Raise the benign TLS close-notify error seen from CDN peers."""
        raise ssl.SSLError("application data after close notify")


class SlowClosingWriter(Writer):
    """Simulate an upstream writer that never completes TLS shutdown promptly."""

    async def wait_closed(self) -> None:
        """Delay cleanup beyond the response deadline."""
        await asyncio.sleep(1)


class SlowReader(Reader):
    """Provide a body reader that exceeds the configured read timeout."""

    async def read(self, size: int) -> bytes:
        """Sleep beyond the response deadline before reading."""
        await asyncio.sleep(1)
        return await super().read(size)


class FailingReader(Reader):
    """Raise a transport error while reading a media body."""

    async def read(self, size: int) -> bytes:
        """Raise a private socket error from the body read."""
        raise OSError("private body socket detail")


class ChunkedReader(Reader):
    """Track exact chunk reads for bounded chunked-response tests."""

    def __init__(self, body: bytes) -> None:
        """Store a chunk framing body and read tracking."""
        super().__init__(body)
        self.exact_reads: list[int] = []

    async def readexactly(self, size: int) -> bytes:
        """Track and return one exact chunk section."""
        self.exact_reads.append(size)
        return await super().readexactly(size)

    async def readuntil(self, separator: bytes) -> bytes:
        """Consume one delimiter-terminated chunk framing line."""
        index = self.body.find(separator)
        if index < 0:
            raise asyncio.IncompleteReadError(self.body, None)
        end = index + len(separator)
        data, self.body = self.body[:end], self.body[end:]
        return data


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
async def test_response_close_ignores_upstream_peer_reset() -> None:
    """Verify normal cleanup does not turn an upstream peer reset into a failure."""
    response = MediaResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "3"},
        Reader(b"body"),
        ResettingWriter(),
        max_bytes=100,
    )

    await response.close()


@pytest.mark.asyncio
async def test_response_close_ignores_upstream_tls_shutdown_error() -> None:
    """Verify normal cleanup tolerates a TLS close-notify error from a CDN peer."""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "content-length": "4"},
        Reader(b"body"),
        TlsClosingWriter(),
        max_bytes=100,
    )

    await response.close()


@pytest.mark.asyncio
async def test_response_close_respects_total_deadline() -> None:
    """Verify response cleanup cannot outlive its configured total deadline."""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "content-length": "4"},
        Reader(b"body"),
        SlowClosingWriter(),
        max_bytes=100,
        total_timeout=0.01,
    )

    await asyncio.wait_for(response.close(), timeout=0.1)


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
async def test_download_rejects_content_type_mismatched_with_token_class() -> None:
    """Verify download streaming enforces the private token media class."""
    response = make_response(b"\xff\xd8\xffimage", content_type="image/jpeg")

    with pytest.raises(AppError) as exc_info:
        _ = [
            chunk
            async for chunk in iter_validated_body(
                response, preview=False, expected_media_class="video"
            )
        ]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_response_byte_limit_terminates_stream() -> None:
    """Verify oversized upstream bodies are stopped at the configured limit."""
    response = make_response(b"\x00" * 101, content_type="video/mp4", max_bytes=100)

    with pytest.raises(AppError) as exc_info:
        await collect(response)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_declared_content_length_shortfall_is_rejected() -> None:
    """Verify EOF before a declared body length is treated as a truncated response."""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "content-length": "4"},
        Reader(b"abc"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        await collect(response)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_body_transport_error_is_mapped_to_media_error() -> None:
    """Verify body transport errors never become a successful truncated stream."""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4"},
        FailingReader(b"body"),
        Writer(),
        max_bytes=100,
    )

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


@pytest.mark.asyncio
async def test_total_response_timeout_terminates_trickle_body() -> None:
    """Verify a body cannot hold a download slot beyond its total deadline."""
    response = MediaResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "4"},
        SlowReader(b"body"),
        Writer(),
        max_bytes=100,
        read_timeout=1.0,
        total_timeout=0.01,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in iter_validated_body(response)]

    assert exc_info.value.code == "upstream_media_invalid"


def test_download_filename_is_sanitized() -> None:
    """Verify download response headers cannot inject a filename."""
    headers = build_download_headers("../evil\r\n.txt")

    assert ".." not in headers["Content-Disposition"]
    assert "\r" not in headers["Content-Disposition"]


def test_preview_response_headers_do_not_forward_upstream_content_length() -> None:
    """preview response 不得轉送 upstream Content-Length。"""
    response = make_response(b"body", content_type="image/jpeg")

    headers = build_forward_response_headers(response, filename="image.jpg", preview=True)

    assert "Content-Length" not in headers


@pytest.mark.parametrize("content_encoding", ["gzip", "br", "identity, gzip"])
def test_media_response_rejects_non_identity_content_encoding(content_encoding: str) -> None:
    """MediaResponse 不得接受會讓 raw media bytes 被誤標的壓縮編碼。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(
            200,
            {"content-type": "image/jpeg", "content-encoding": content_encoding},
            Reader(b"\xff\xd8\xffbody"),
            Writer(),
            max_bytes=100,
        )

    assert exc_info.value.code == "upstream_media_invalid"


def test_media_response_normalizes_identity_content_encoding() -> None:
    """MediaResponse 應接受並正規化 case/OWS 形式的 identity。"""
    response = MediaResponse(
        200,
        {"content-type": "image/jpeg", "Content-Encoding": "\t IdEnTiTy \t"},
        Reader(b"\xff\xd8\xffbody"),
        Writer(),
        max_bytes=100,
    )

    assert response.headers["content-encoding"] == "identity"


@pytest.mark.parametrize("transfer_encoding", ["gzip", "gzip, chunked"])
def test_forward_headers_reject_unsupported_transfer_encoding(transfer_encoding: str) -> None:
    """MediaResponse constructor 遇到不支援的 transfer coding 時應 fail closed。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(
            200,
            {"content-type": "image/jpeg", "transfer-encoding": transfer_encoding},
            Reader(b"body"),
            Writer(),
            max_bytes=100,
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_response_rejects_conflicting_framing_headers() -> None:
    """MediaResponse constructor 遇到 CL 與 TE 共存時應 fail closed。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(
            200,
            {
                "content-type": "video/mp4",
                "content-length": "2",
                "transfer-encoding": "chunked",
            },
            ChunkedReader(b"2\r\nab\r\n0\r\n\r\n"),
            Writer(),
            max_bytes=100,
        )

    assert exc_info.value.code == "upstream_media_invalid"


def test_forward_headers_reject_conflicting_framing_headers() -> None:
    """direct response fixture 遇到 CL 與 TE 共存時不得建立。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(
            200,
            {
                "content-type": "video/mp4",
                "content-length": "2",
                "transfer-encoding": "chunked",
            },
            Reader(b""),
            Writer(),
            max_bytes=100,
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "chunk_size",
    [b"+2", b"2 ", b" 2", b"0x2", b""],
    ids=["plus", "trailing-ows", "leading-ows", "hex-prefix", "empty"],
)
@pytest.mark.asyncio
async def test_chunked_response_rejects_non_strict_chunk_size(chunk_size: bytes) -> None:
    """chunk size 必須是非空且只含 ASCII hex digits 的 token。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(chunk_size + b"\r\nab\r\n0\r\n\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "extension",
    [
        b";bad\x00=value",
        b';name="quoted"',
        b";=value",
        b";name=",
        b";name=value\xff",
        b";name value",
    ],
    ids=["nul", "quoted", "missing-name", "missing-value", "non-ascii", "ows"],
)
@pytest.mark.asyncio
async def test_chunked_response_rejects_invalid_chunk_extensions(extension: bytes) -> None:
    """chunk extension 只允許嚴格的 token 或 token-valued 格式。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"2" + extension + b"\r\nab\r\n0\r\n\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_chunked_response_accepts_token_chunk_extensions() -> None:
    """chunk extension 可使用無引號 token 與 token-valued extension。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"2;flag;name=value\r\nab\r\n0\r\n\r\n"),
        Writer(),
        max_bytes=100,
    )

    assert b"".join([chunk async for chunk in response.iter_bytes()]) == b"ab"


@pytest.mark.asyncio
async def test_chunked_response_consumes_complete_trailer_section() -> None:
    """zero chunk 後應讀到 trailer section 的終止空 CRLF。"""
    reader = ChunkedReader(b"2\r\nab\r\n0\r\nX-Trailer: ok\r\n\r\n")
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        reader,
        Writer(),
        max_bytes=100,
    )

    body = b"".join([chunk async for chunk in response.iter_bytes()])

    assert body == b"ab"
    assert reader.body == b""


@pytest.mark.asyncio
async def test_chunked_response_rejects_missing_trailer_terminator() -> None:
    """缺少 trailer section 終止 CRLF 時應拒絕 response。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"2\r\nab\r\n0\r\nX-Trailer: missing-end\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_chunked_response_rejects_invalid_trailer_field() -> None:
    """zero chunk 後的 trailer 必須是合法 header field-name/value。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"0\r\nBad Header: value\r\n\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "Content-Length",
        "Transfer-Encoding",
        "Host",
        "Connection",
        "TE",
        "Trailer",
        "Upgrade",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "Keep-Alive",
        "Authorization",
        "Cache-Control",
        "Content-Type",
        "Content-Encoding",
        "Content-Range",
    ],
)
@pytest.mark.asyncio
async def test_chunked_response_rejects_forbidden_trailer_fields(forbidden_field: str) -> None:
    """chunk trailer 不得攜帶 framing、connection 或 proxy control 欄位。"""
    trailer = f"{forbidden_field}: value\r\n".encode("ascii")
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"0\r\n" + trailer + b"\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_chunked_response_rejects_oversized_aggregate_trailers() -> None:
    """trailer section 累計超過固定上限時應立即拒絕。"""
    first = b"X-First: " + b"a" * 4090 + b"\r\n"
    second = b"X-Second: " + b"b" * 4090 + b"\r\n"
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        ChunkedReader(b"0\r\n" + first + second + b"\r\n"),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_chunked_response_rejects_declared_chunk_before_reading_it() -> None:
    """Verify a chunk larger than the limit is rejected before buffering its payload."""
    reader = ChunkedReader(b"100\r\n" + b"x" * 256 + b"\r\n0\r\n\r\n")
    response = MediaResponse(
        200,
        {"content-type": "image/jpeg", "transfer-encoding": "chunked"},
        reader,
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes(max_bytes=10)]

    assert exc_info.value.code == "upstream_media_invalid"
    assert reader.exact_reads == []


@pytest.mark.asyncio
async def test_chunked_response_reads_large_chunks_in_bounded_pieces() -> None:
    """Verify one large HTTP chunk cannot become one unbounded preview buffer."""
    body = b"x" * 1024
    reader = ChunkedReader(b"400\r\n" + body + b"\r\n0\r\n\r\n")
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
        reader,
        Writer(),
        max_bytes=2048,
    )

    streamed = b"".join([chunk async for chunk in response.iter_bytes(chunk_size=128)])

    assert streamed == body
    assert reader.exact_reads[:8] == [128] * 8
