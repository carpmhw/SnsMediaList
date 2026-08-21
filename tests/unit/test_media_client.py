"""Tests for safe media destination and response validation."""

import asyncio
import ipaddress
import time
from typing import Any

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.media_client import (
    MediaClient,
    MediaDestinationPolicy,
    MediaResponse,
    MediaTruncatedError,
    ParsedContentRange,
    _parse_content_range,
    _parse_response_headers,
    build_preview_headers,
    connection_target,
    validate_preview_signature,
    validate_resume_response,
)


def public_resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return a deterministic public address for destination tests."""
    return [ipaddress.ip_address("93.184.216.34")]


def test_media_client_disables_total_timeout_by_default() -> None:
    """MediaClient 未指定 total timeout 時不得建立完整回應 deadline。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    client = MediaClient(policy)

    assert client.total_timeout is None


def test_media_policy_accepts_allowed_https_cdn() -> None:
    """Verify an approved X CDN URL returns a pinned hostname and IP."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    target = policy.validate_url("https://pbs.twimg.com/media/1.jpg?name=orig")

    assert target.hostname == "pbs.twimg.com"
    assert str(target.address) == "93.184.216.34"


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/1.jpg",
        "https://pbs.twimg.com.evil.example/media/1.jpg",
        "https://user:pass@pbs.twimg.com/media/1.jpg",
        "https://pbs.twimg.com:8443/media/1.jpg",
        "https://pbs.twimg.com/media/1.jpg#fragment",
        "https://pbs.twimg.com/media/1.jpg\r\nX-Injected: value",
        "https://pbs.twimg.com/media/é.jpg",
    ],
)
def test_media_policy_rejects_unsafe_url_components(url: str) -> None:
    """Verify media destinations require a clean HTTPS URL on an approved host."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate_url(url)

    assert exc_info.value.code == "unsafe_destination"


def test_media_policy_rejects_one_unsafe_dns_answer() -> None:
    """Verify one non-public DNS answer rejects a mixed answer set."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=lambda _host, _port: [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("127.0.0.1"),
        ],
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate_url("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "unsafe_destination"


def test_connection_target_preserves_hostname_for_tls_and_host() -> None:
    """Verify transport parameters use the selected IP with original hostname."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    target = policy.validate_url("https://pbs.twimg.com/media/1.jpg")

    connection = connection_target(target)

    assert connection.address == "93.184.216.34"
    assert connection.server_hostname == "pbs.twimg.com"
    assert connection.host_header == "pbs.twimg.com"


def test_preview_signature_accepts_jpeg_and_rejects_html() -> None:
    """Verify preview content is checked using MIME and magic bytes."""
    assert validate_preview_signature("image/jpeg", b"\xff\xd8\xff\xe0rest") is True
    assert validate_preview_signature("image/jpeg", b"<html>") is False
    assert validate_preview_signature("image/svg+xml", b"<svg>") is False


def test_preview_headers_are_passive() -> None:
    """Verify preview responses include inline disposition and isolation headers."""
    headers = build_preview_headers("poster.jpg")

    assert headers["Content-Disposition"].startswith("inline;")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


class FakeResponseReader:
    """Provide one deterministic HTTP response for transport tests."""

    def __init__(self, header_block: bytes, body: bytes = b"") -> None:
        """Store response headers and body bytes."""
        self.header_block = header_block
        self.body = body

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return the configured response headers."""
        return self.header_block

    async def readexactly(self, size: int) -> bytes:
        """Return exactly the requested body bytes."""
        data, self.body = self.body[:size], self.body[size:]
        return data

    async def read(self, size: int) -> bytes:
        """Return the remaining response body or EOF."""
        data, self.body = self.body[:size], self.body[size:]
        return data


class FakeResponseWriter:
    """Capture request bytes and TLS connection cleanup."""

    def __init__(self) -> None:
        """Initialize captured request state."""
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        """Capture bytes written to the fake upstream."""
        self.writes.append(value)

    async def drain(self) -> None:
        """Complete a fake write."""

    def close(self) -> None:
        """Close the fake upstream connection."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


class RuntimeCloseWriter(FakeResponseWriter):
    """模擬同步 writer.close 拋出的 raw runtime 例外。"""

    def close(self) -> None:
        """拋出不應穿透 response cleanup 的 runtime 例外。"""
        raise RuntimeError("private synchronous close detail")


class OSErrorCloseWriter(FakeResponseWriter):
    """模擬同步 writer.close 拋出的 raw OS 例外。"""

    def close(self) -> None:
        """拋出不應穿透 response cleanup 的 OS 例外。"""
        raise OSError("private synchronous close detail")


class RuntimeWaitClosedWriter(FakeResponseWriter):
    """模擬非同步 writer.wait_closed 拋出的 raw runtime 例外。"""

    async def wait_closed(self) -> None:
        """拋出不應穿透 response cleanup 的 runtime 例外。"""
        raise RuntimeError("private asynchronous close detail")


class RuntimeWriteWriter(FakeResponseWriter):
    """模擬 request writer.write 拋出的 raw runtime 例外。"""

    def write(self, value: bytes) -> None:
        """拋出應映射成 generic upstream failure 的 runtime 例外。"""
        _ = value
        raise RuntimeError("private request write detail")


class RuntimeDrainWriter(FakeResponseWriter):
    """模擬 request writer.drain 拋出的 raw runtime 例外。"""

    async def drain(self) -> None:
        """拋出應映射成 generic upstream failure 的 runtime 例外。"""
        raise RuntimeError("private request drain detail")


class RuntimeHeaderReader(FakeResponseReader):
    """模擬 response header read 拋出的 raw runtime 例外。"""

    async def readuntil(self, _separator: bytes) -> bytes:
        """拋出應映射成安全 header failure 的 runtime 例外。"""
        raise RuntimeError("private response header detail")


class FailingRedirectResponse(MediaResponse):
    """模擬 redirect response cleanup 拋出 raw transport 例外。"""

    def __init__(self, error_type: type[BaseException]) -> None:
        """建立帶有 redirect location 與受控 close 失敗的 response。"""
        super().__init__(
            302,
            {"location": "https://pbs.twimg.com/media/redirected.jpg"},
            FakeResponseReader(b""),
            FakeResponseWriter(),
            max_bytes=100,
        )
        self.error_type = error_type

    async def close(self) -> None:
        """拋出不應覆蓋安全 redirect error 的 cleanup 例外。"""
        raise self.error_type("private redirect close detail")


def test_response_header_parser_rejects_conflicting_content_lengths() -> None:
    """原始 response 含衝突 Content-Length 時應 fail closed。"""
    raw_headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: video/mp4\r\n"
        b"Content-Length: 3\r\n"
        b"Content-Length: 4\r\n\r\n"
    )

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_content_range_parser_accepts_valid_range() -> None:
    """Content-Range parser 應接受嚴格的完整 byte range。"""
    parsed = _parse_content_range("bytes 4-9/10")

    assert parsed == ParsedContentRange(start=4, end=9, total=10)


@pytest.mark.parametrize(
    "value",
    [
        "bytes */123",
        "bytes 10-9/123",
        "bytes 10-20/*",
        "bytes +10-20/123",
        "bytes 010-20/123",
        "bytes 10-020/123",
        "bytes 10-20/0123",
        "bytes 10-20/20",
        "bytes 10-20/123 extra",
        "bytes 10-20/123, bytes 30-40/123",
        "bytes 10-é/123",
    ],
    ids=[
        "unsatisfied",
        "end-before-start",
        "wildcard-total",
        "plus-sign",
        "leading-zero-start",
        "leading-zero-end",
        "leading-zero-total",
        "total-not-greater-than-end",
        "extra-token",
        "multiple-ranges",
        "non-ascii",
    ],
)
def test_content_range_parser_rejects_invalid_syntax(value: str) -> None:
    """Content-Range parser 應拒絕不完整、寬鬆或含額外 token 的格式。"""
    with pytest.raises(AppError) as exc_info:
        _parse_content_range(value)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_response_raises_typed_error_for_early_eof() -> None:
    """合法 Content-Length 提前 EOF 應保留可分類的 truncation error。"""
    response = MediaResponse(
        200,
        {"content-type": "video/mp4", "content-length": "6"},
        FakeResponseReader(b"abcd"),
        FakeResponseWriter(),
        max_bytes=100,
    )

    with pytest.raises(MediaTruncatedError) as exc_info:
        _ = [chunk async for chunk in response.iter_bytes()]

    assert exc_info.value.code == "upstream_media_invalid"
    assert exc_info.value.message == "The media response body was truncated."


def test_response_header_parser_rejects_content_length_and_transfer_encoding() -> None:
    """response 同時宣告 Content-Length 與 Transfer-Encoding 時應拒絕。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "content_length",
    [b"", b"+1", b"1.0", b"1a", b"\xff"],
    ids=["empty", "plus", "fraction", "alpha", "obs-text"],
)
def test_response_header_parser_rejects_invalid_content_length(content_length: bytes) -> None:
    """parser 應只接受 ASCII decimal digits 的 Content-Length。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nContent-Length: " + content_length + b"\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize("status_text", [b"+200", b"0200", b"020", b"20a", b"600", b"099"])
def test_response_header_parser_rejects_non_three_digit_status(status_text: bytes) -> None:
    """status line 應只接受 HTTP/1.1 後恰好三個 ASCII decimal digits。"""
    raw_headers = b"HTTP/1.1 " + status_text + b" OK\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize("status_line", [b"HTTP/1.1 200", b"HTTP/1.1 599"])
def test_response_header_parser_requires_reason_separator(status_line: bytes) -> None:
    """status code 後缺少 reason separator 時應拒絕 response。"""
    raw_headers = status_line + b"\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize("status_text", [b"100", b"599"])
def test_response_header_parser_accepts_inclusive_status_code_range(status_text: bytes) -> None:
    """status code 100 與 599 應是合法的 inclusive 邊界。"""
    raw_headers = b"HTTP/1.1 " + status_text + b" OK\r\n\r\n"

    status_code, _headers = _parse_response_headers(raw_headers)

    assert status_code == int(status_text)


@pytest.mark.parametrize("content_encoding", [b"gzip", b"br", b"identity, gzip"])
def test_response_header_parser_rejects_non_identity_content_encoding(
    content_encoding: bytes,
) -> None:
    """response 只允許 identity，避免壓縮 bytes 被當作 raw media 轉送。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nContent-Encoding: " + content_encoding + b"\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_response_header_parser_normalizes_identity_content_encoding() -> None:
    """Content-Encoding 的 case 與 ASCII OWS 應正規化為 identity。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nContent-Encoding:\t IdEnTiTy \t\r\n\r\n"

    _status_code, headers = _parse_response_headers(raw_headers)

    assert headers["content-encoding"] == "identity"


def test_response_header_parser_trims_ascii_content_length_ows() -> None:
    """parser 可移除 Content-Length value 外側的 ASCII OWS。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nContent-Length:\t 4 \t\r\n\r\n"

    _status_code, headers = _parse_response_headers(raw_headers)

    assert headers["content-length"] == "4"


@pytest.mark.parametrize("control", [b"\x00", b"\x1f", b"\x7f"])
def test_response_header_parser_rejects_ctl_header_values(control: bytes) -> None:
    """response header value 含 CTL 或 DEL 時應拒絕以避免注入。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nX-Test: safe" + control + b"value\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_response_header_parser_accepts_htab_and_iso_8859_obs_text() -> None:
    """header value 應保留允許的 HTAB 與 ISO-8859-1 obs-text。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nX-Test: obs-\t\x80\r\n\r\n"

    _status_code, headers = _parse_response_headers(raw_headers)

    assert headers["x-test"] == "obs-\t\x80"


@pytest.mark.parametrize(
    "header_line",
    [
        b"Content-Length : 4",
        b" Content-Length: 4",
        b"Content@Length: 4",
    ],
    ids=["trailing-ows", "leading-ows", "invalid-token-character"],
)
def test_response_header_parser_rejects_invalid_field_names(header_line: bytes) -> None:
    """response header 名稱含 OWS 或非法 token 字元時應拒絕。"""
    raw_headers = b"HTTP/1.1 200 OK\r\n" + header_line + b"\r\n\r\n"

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_response_header_parser_rejects_duplicate_transfer_encoding() -> None:
    """重複 Transfer-Encoding header 不得被後值靜默覆蓋。"""
    raw_headers = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n"
    )

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_response_header_parser_rejects_duplicate_content_range() -> None:
    """重複 Content-Range 不得被後值靜默覆蓋。"""
    raw_headers = (
        b"HTTP/1.1 206 Partial Content\r\n"
        b"Content-Range: bytes 4-9/10\r\n"
        b"Content-Range: bytes 4-9/10\r\n\r\n"
    )

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "transfer_encoding",
    ["gzip", "gzip, chunked", "chunked, gzip", "chunked, chunked", ""],
)
def test_response_header_parser_rejects_unsupported_transfer_encoding(
    transfer_encoding: str,
) -> None:
    """不支援或非單一 chunked 的 transfer coding 應 fail closed。"""
    raw_headers = (f"HTTP/1.1 200 OK\r\nTransfer-Encoding: {transfer_encoding}\r\n\r\n").encode(
        "ascii"
    )

    with pytest.raises(AppError) as exc_info:
        _parse_response_headers(raw_headers)

    assert exc_info.value.code == "upstream_media_invalid"


def test_response_header_parser_accepts_one_chunked_transfer_encoding() -> None:
    """單一大小寫混合且含 ASCII OWS 的 chunked coding 應被接受。"""
    raw_headers = b"HTTP/1.1 200 OK\r\nTransfer-Encoding:\t ChUnKeD \t\r\n\r\n"

    _status_code, headers = _parse_response_headers(raw_headers)

    assert headers["transfer-encoding"] == "ChUnKeD"


def test_media_response_normalizes_direct_header_fixture() -> None:
    """MediaResponse constructor 應 canonicalize 名稱並移除 ASCII OWS。"""
    response = MediaResponse(
        200,
        {"Content-Length": "\t3 \t", "X-Test": "\tvalue\t"},
        FakeResponseReader(b"abc"),
        FakeResponseWriter(),
        max_bytes=100,
    )

    assert response.headers == {"content-length": "3", "x-test": "value"}
    assert response.content_length == 3


def test_media_response_rejects_direct_conflicting_case_mixed_framing() -> None:
    """大小寫混合的 direct Content-Length/Transfer-Encoding 不得繞過 framing 驗證。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(
            200,
            {"Content-Length": "3", "Transfer-Encoding": "chunked"},
            FakeResponseReader(b"0\r\n\r\n"),
            FakeResponseWriter(),
            max_bytes=100,
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize(
    "headers",
    [
        {"Content@Length": "3"},
        {"X-Test": "bad\x00value"},
    ],
    ids=["invalid-field-name", "control-value"],
)
def test_media_response_rejects_invalid_direct_headers(headers: dict[str, str]) -> None:
    """direct response fixture 的 field-name/value 也必須經過安全驗證。"""
    with pytest.raises(AppError) as exc_info:
        MediaResponse(200, headers, FakeResponseReader(b""), FakeResponseWriter(), max_bytes=100)

    assert exc_info.value.code == "upstream_media_invalid"


class SlowHeaderReader(FakeResponseReader):
    """Delay response headers so cancellation occurs during header parsing."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Wait until the transport request is cancelled."""
        await asyncio.sleep(1)
        return await super().readuntil(_separator)


class CancellationResistantReader(FakeResponseReader):
    """模擬取消後仍等待釋放訊號的 response reader operation。"""

    def __init__(self) -> None:
        """初始化 reader 的 cancellation lifecycle 事件。"""
        super().__init__(b"")
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def _wait_for_release(self, result: bytes) -> bytes:
        """等待外部釋放，並在取消後仍完成 operation。"""
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        finally:
            self.finished.set()
        return result

    async def read(self, size: int) -> bytes:
        """執行 cancellation-resistant 的 body read。"""
        return await self._wait_for_release(b"x"[:size])

    async def readuntil(self, separator: bytes) -> bytes:
        """執行 cancellation-resistant 的 delimiter read。"""
        return await self._wait_for_release(b"line" + separator)

    async def readexactly(self, size: int) -> bytes:
        """執行 cancellation-resistant 的 exact read。"""
        return await self._wait_for_release(b"x" * size)


class RawTimeoutReader(FakeResponseReader):
    """模擬 reader operation 自己拋出的 raw TimeoutError。"""

    async def read(self, size: int) -> bytes:
        """拋出 upstream 自己產生的 timeout。"""
        _ = size
        raise TimeoutError("raw reader timeout")

    async def readuntil(self, _separator: bytes) -> bytes:
        """拋出 upstream 自己產生的 delimiter timeout。"""
        raise TimeoutError("raw reader timeout")

    async def readexactly(self, size: int) -> bytes:
        """拋出 upstream 自己產生的 exact-read timeout。"""
        _ = size
        raise TimeoutError("raw reader timeout")


class RaisingCloseWriter(FakeResponseWriter):
    """模擬 close 自己失敗的 upstream writer。"""

    def close(self) -> None:
        """拋出不應覆蓋 primary request error 的 cleanup 例外。"""
        raise RuntimeError("private writer close detail")


class CancellationResistantGate:
    """模擬 transport operation 在取消後仍等待外部釋放。"""

    def __init__(self) -> None:
        """初始化 transport operation lifecycle 事件。"""
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        """等待釋放訊號，並記錄 cancellation-resistant lifecycle。"""
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        finally:
            self.finished.set()


class DetachedResultWriter(FakeResponseWriter):
    """追蹤 detached open connection result 的 writer cleanup。"""

    def __init__(self) -> None:
        """初始化 writer 關閉與 wait_closed 計數。"""
        super().__init__()
        self.close_calls = 0
        self.wait_closed_calls = 0
        self.close_event = asyncio.Event()
        self.wait_closed_event = asyncio.Event()

    def close(self) -> None:
        """記錄 detached connection 被關閉。"""
        self.close_calls += 1
        super().close()
        self.close_event.set()

    async def wait_closed(self) -> None:
        """記錄 detached connection 的 bounded close wait。"""
        self.wait_closed_calls += 1
        self.wait_closed_event.set()


class CancellationResistantDrainWriter(FakeResponseWriter):
    """模擬 writer.drain 在取消後仍不立即結束。"""

    def __init__(self, gate: CancellationResistantGate) -> None:
        """保存 drain operation 的 lifecycle gate。"""
        super().__init__()
        self.gate = gate

    async def drain(self) -> None:
        """等待 cancellation-resistant drain operation 完成。"""
        await self.gate.wait()


class CancellationResistantHeaderReader(FakeResponseReader):
    """模擬 response header read 在取消後仍不立即結束。"""

    def __init__(self, gate: CancellationResistantGate) -> None:
        """保存 header read 的 lifecycle gate。"""
        super().__init__(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        self.gate = gate

    async def readuntil(self, separator: bytes) -> bytes:
        """等待 cancellation-resistant header read 完成。"""
        await self.gate.wait()
        return await super().readuntil(separator)


class FailingHeaderReader(FakeResponseReader):
    """Raise a transport error while parsing upstream response headers."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Raise a private socket error from the header read."""
        raise OSError("private header socket detail")


class IncompleteHeaderReader(FakeResponseReader):
    """Raise an EOF before the upstream response header terminator."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Raise an incomplete header read."""
        raise asyncio.IncompleteReadError(b"HTTP/1.1 200", 32)


class LimitHeaderReader(FakeResponseReader):
    """Raise a bounded line-length failure while reading response headers."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Raise the stream line limit error."""
        raise asyncio.LimitOverrunError("header too long", 0)


class SlowWriter(FakeResponseWriter):
    """Delay request draining to exercise cancellation cleanup."""

    async def drain(self) -> None:
        """Wait until the transport request is cancelled."""
        await asyncio.sleep(1)


class SlowCloseWriter(FakeResponseWriter):
    """Delay writer cleanup beyond the request deadline."""

    async def wait_closed(self) -> None:
        """Wait indefinitely until the caller cancels cleanup."""
        await asyncio.sleep(1)


class CancellationResistantCloseWriter(FakeResponseWriter):
    """模擬取消後仍等待訊號且最終拋出例外的 writer cleanup。"""

    def __init__(self) -> None:
        """初始化 writer cleanup 的同步事件。"""
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.release_close = asyncio.Event()

    async def wait_closed(self) -> None:
        """等待釋放訊號，並在取消後延遲完成以測試 detached cleanup。"""
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            await self.release_close.wait()
        finally:
            self.close_finished.set()
        raise RuntimeError("detached writer cleanup failure")


class FailingCloseWriter(FakeResponseWriter):
    """Raise a socket cleanup error after the writer is closed."""

    async def wait_closed(self) -> None:
        """Raise a transport error that cleanup should safely absorb."""
        raise OSError("private socket cleanup detail")


@pytest.mark.parametrize("method", ["read", "readuntil", "readexactly"])
@pytest.mark.asyncio
async def test_media_response_read_timeout_detaches_resistant_reader(method: str) -> None:
    """body 與 framing read 到期時不得等待 cancellation-resistant reader。"""
    reader = CancellationResistantReader()
    response = MediaResponse(
        200,
        {"content-type": "video/mp4"},
        reader,
        FakeResponseWriter(),
        max_bytes=100,
        read_timeout=0.01,
    )
    if method == "read":
        task = asyncio.create_task(response._read(1))
    elif method == "readuntil":
        task = asyncio.create_task(response._read_until(b"\r\n"))
    else:
        task = asyncio.create_task(response._read_exactly(1))

    try:
        await reader.started.wait()
        await asyncio.wait_for(reader.cancelled.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError, match="timed out"):
            task.result()
    finally:
        reader.release.set()
        if not reader.finished.is_set():
            await asyncio.wait_for(reader.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass
        else:
            try:
                task.result()
            except BaseException:
                pass


@pytest.mark.parametrize("method", ["read", "readuntil", "readexactly"])
@pytest.mark.asyncio
async def test_media_response_maps_raw_reader_timeout_to_safe_failure(method: str) -> None:
    """reader 自己拋出的 raw TimeoutError 應映射為安全 upstream failure。"""
    reader = RawTimeoutReader(b"")
    response = MediaResponse(
        200,
        {"content-type": "video/mp4"},
        reader,
        FakeResponseWriter(),
        max_bytes=100,
    )
    if method == "read":
        operation = response._read(1)
    elif method == "readuntil":
        operation = response._read_until(b"\r\n")
    else:
        operation = response._read_exactly(1)

    with pytest.raises(AppError, match="could not be read") as exc_info:
        await operation

    assert "timed out" not in exc_info.value.message
    assert exc_info.value.message == "The media response could not be read."
    assert "raw reader timeout" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_client_maps_raw_validation_timeout_to_request_failure() -> None:
    """validation 自己拋出的 raw timeout 應映射為安全 request failure。"""

    def raw_timeout_resolver(
        _host: str, _port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """模擬 DNS resolver 自己拋出的 timeout。"""
        raise TimeoutError("private resolver timeout")

    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=raw_timeout_resolver,
    )

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy)._validate_target(
            "https://pbs.twimg.com/media/1.jpg", deadline=None
        )

    assert exc_info.value.message == "The upstream media request failed."
    assert "private resolver timeout" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_client_maps_raw_connect_timeout_to_request_failure(monkeypatch: Any) -> None:
    """connect operation 自己拋出的 raw timeout 應映射為安全 request failure。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    async def raw_timeout_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """模擬 socket connect 自己拋出的 timeout。"""
        raise TimeoutError("private connect timeout")

    monkeypatch.setattr(asyncio, "open_connection", raw_timeout_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media request failed."
    assert "private connect timeout" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_client_maps_raw_header_timeout_to_safe_header_failure(
    monkeypatch: Any,
) -> None:
    """header operation 自己拋出的 raw timeout 應映射為安全 header failure。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def raw_timeout_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 header read 自己拋出 timeout 的 transport。"""
        return RawTimeoutReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", raw_timeout_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media headers were invalid."
    assert "raw reader timeout" not in str(exc_info.value)
    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_fetch_maps_raw_timeout_without_exposing_cause(
    monkeypatch: Any,
) -> None:
    """fetch 邊界收到 raw timeout 時應回傳安全 request failure。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    client = MediaClient(policy)

    async def raw_timeout_fetch_target(*_args: Any, **_kwargs: Any) -> MediaResponse:
        """模擬底層 fetch operation 自己拋出的 timeout。"""
        raise TimeoutError("private fetch timeout")

    monkeypatch.setattr(client, "_fetch_target", raw_timeout_fetch_target)

    with pytest.raises(AppError) as exc_info:
        await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media request failed."
    assert "private fetch timeout" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_client_close_failure_does_not_replace_raw_header_error(
    monkeypatch: Any,
) -> None:
    """header primary error 不得被 writer close cleanup 例外覆蓋。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    async def raw_timeout_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳會在 header 與 close 階段各自失敗的 transport。"""
        return RawTimeoutReader(b""), RaisingCloseWriter()

    monkeypatch.setattr(asyncio, "open_connection", raw_timeout_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media headers were invalid."
    assert "private writer close detail" not in str(exc_info.value)


@pytest.mark.parametrize("writer_type", [RuntimeCloseWriter, OSErrorCloseWriter])
@pytest.mark.asyncio
async def test_media_response_close_absorbs_synchronous_writer_errors(
    writer_type: type[FakeResponseWriter],
) -> None:
    """response cleanup 遇到同步 writer raw 例外時不得 raw escape。"""
    response = MediaResponse(
        200,
        {},
        FakeResponseReader(b""),
        writer_type(),
        max_bytes=100,
    )

    await response.close()


@pytest.mark.asyncio
async def test_media_response_close_absorbs_wait_closed_runtime_error() -> None:
    """response cleanup 遇到 wait_closed runtime 例外時不得 raw escape。"""
    response = MediaResponse(
        200,
        {},
        FakeResponseReader(b""),
        RuntimeWaitClosedWriter(),
        max_bytes=100,
    )

    await response.close()


@pytest.mark.parametrize("writer_type", [RuntimeWriteWriter, RuntimeDrainWriter])
@pytest.mark.asyncio
async def test_media_client_maps_raw_request_runtime_errors_to_safe_failure(
    monkeypatch: Any,
    writer_type: type[FakeResponseWriter],
) -> None:
    """request write 或 drain 的 raw runtime 例外應映射成 generic upstream failure。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = writer_type()

    async def runtime_request_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 request operation 會失敗的受控 transport。"""
        return FakeResponseReader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"), writer

    monkeypatch.setattr(asyncio, "open_connection", runtime_request_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media request failed."
    assert "private request" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_client_maps_raw_header_runtime_error_to_safe_failure(monkeypatch: Any) -> None:
    """response header read 的 raw runtime 例外應映射成安全 header failure。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def runtime_header_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 header read 會失敗的受控 transport。"""
        return RuntimeHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", runtime_header_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.message == "The upstream media headers were invalid."
    assert "private response header detail" not in str(exc_info.value)
    assert writer.closed is True


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
@pytest.mark.asyncio
async def test_media_client_redirect_close_failure_preserves_safe_error(
    monkeypatch: Any,
    error_type: type[BaseException],
) -> None:
    """redirect response close 失敗時仍應回傳安全 redirect error。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    response = FailingRedirectResponse(error_type)
    client = MediaClient(policy, max_redirects=0)

    async def redirect_target(*_args: Any, **_kwargs: Any) -> MediaResponse:
        """回傳 close 失敗的 redirect response。"""
        return response

    monkeypatch.setattr(client, "_fetch_target", redirect_target)

    with pytest.raises(AppError) as exc_info:
        await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "unsafe_destination"
    assert exc_info.value.message == "The media response redirected too many times."
    assert "private redirect close detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_media_response_ignores_socket_cleanup_oserror() -> None:
    """Verify an upstream socket close error cannot escape response cleanup."""
    writer = FailingCloseWriter()
    response = MediaResponse(
        200,
        {},
        FakeResponseReader(b""),
        writer,
        max_bytes=100,
    )

    await response.close()

    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_response_close_detaches_cancellation_resistant_writer() -> None:
    """response close 到期時應立即返回並觀察 detached writer 例外。"""
    writer = CancellationResistantCloseWriter()
    response = MediaResponse(
        200,
        {},
        FakeResponseReader(b""),
        writer,
        max_bytes=100,
        read_timeout=0.01,
    )
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 response close observer 消費的 writer 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    task = asyncio.create_task(response.close())
    try:
        await writer.close_started.wait()
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        assert task.exception() is None
        assert writer.closed is True
        await asyncio.wait_for(writer.close_cancelled.wait(), timeout=0.1)
        writer.release_close.set()
        await asyncio.wait_for(writer.close_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        writer.release_close.set()
        if not writer.close_finished.is_set():
            await asyncio.wait_for(writer.close_finished.wait(), timeout=0.2)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass
        else:
            try:
                task.result()
            except BaseException:
                pass
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_media_client_connect_timeout_detaches_resistant_operation(monkeypatch: Any) -> None:
    """connect timeout 到期時不得等待 cancellation-resistant open operation。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    gate = CancellationResistantGate()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳一個取消後仍等待的 open connection operation。"""
        await gate.wait()
        return (
            FakeResponseReader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
            FakeResponseWriter(),
        )

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    task = asyncio.create_task(
        MediaClient(policy, connect_timeout=0.01).fetch("https://pbs.twimg.com/media/1.jpg")
    )
    try:
        await gate.started.wait()
        await asyncio.wait_for(gate.cancelled.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError, match="timed out"):
            task.result()
    finally:
        gate.release.set()
        if not gate.finished.is_set():
            await asyncio.wait_for(gate.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                result = await asyncio.wait_for(task, timeout=0.2)
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass
        else:
            try:
                result = task.result()
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass


@pytest.mark.parametrize("cancel_caller", [False, True], ids=["timeout", "caller-cancel"])
@pytest.mark.asyncio
async def test_media_client_closes_detached_open_connection_result(
    monkeypatch: Any,
    cancel_caller: bool,
) -> None:
    """open connection 被脫離後成功回傳的 writer 必須 close 且不得產生未處理例外。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    gate = CancellationResistantGate()
    writer = DetachedResultWriter()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 detached result cleanup observer 消費的例外。"""
        unhandled.append(context)

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """模擬取消後仍成功回傳 fake reader 與 writer 的 open operation。"""
        await gate.wait()
        return FakeResponseReader(b""), writer

    loop.set_exception_handler(exception_handler)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    task = asyncio.create_task(
        MediaClient(policy, connect_timeout=0.01).fetch("https://pbs.twimg.com/media/1.jpg")
    )
    try:
        await gate.started.wait()
        if cancel_caller:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(AppError, match="timed out"):
                await task
        await asyncio.wait_for(gate.cancelled.wait(), timeout=0.1)
        gate.release.set()
        await asyncio.wait_for(gate.finished.wait(), timeout=0.1)
        await asyncio.wait_for(writer.close_event.wait(), timeout=0.1)
        await asyncio.wait_for(writer.wait_closed_event.wait(), timeout=0.1)
        assert writer.close_calls == 1
        assert writer.wait_closed_calls == 1
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        gate.release.set()
        if not gate.finished.is_set():
            await asyncio.wait_for(gate.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_media_client_drain_timeout_detaches_resistant_operation(monkeypatch: Any) -> None:
    """request drain timeout 到期時不得等待 cancellation-resistant writer operation。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    gate = CancellationResistantGate()
    writer = CancellationResistantDrainWriter(gate)

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 drain 會在取消後等待的 upstream transport。"""
        return FakeResponseReader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    task = asyncio.create_task(
        MediaClient(policy, connect_timeout=0.01, read_timeout=0.2).fetch(
            "https://pbs.twimg.com/media/1.jpg"
        )
    )
    try:
        await gate.started.wait()
        await asyncio.wait_for(gate.cancelled.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError, match="timed out"):
            task.result()
        assert writer.closed is True
    finally:
        gate.release.set()
        if not gate.finished.is_set():
            await asyncio.wait_for(gate.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                result = await asyncio.wait_for(task, timeout=0.2)
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass
        else:
            try:
                result = task.result()
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_media_client_header_timeout_detaches_resistant_operation(monkeypatch: Any) -> None:
    """response header timeout 到期時不得等待 cancellation-resistant read operation。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    gate = CancellationResistantGate()
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 header read 會在取消後等待的 upstream transport。"""
        return CancellationResistantHeaderReader(gate), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    task = asyncio.create_task(
        MediaClient(policy, connect_timeout=0.2, read_timeout=0.01).fetch(
            "https://pbs.twimg.com/media/1.jpg"
        )
    )
    try:
        await gate.started.wait()
        await asyncio.wait_for(gate.cancelled.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError, match="timed out"):
            task.result()
        assert writer.closed is True
    finally:
        gate.release.set()
        if not gate.finished.is_set():
            await asyncio.wait_for(gate.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                result = await asyncio.wait_for(task, timeout=0.2)
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass
        else:
            try:
                result = task.result()
                if isinstance(result, MediaResponse):
                    await result.close()
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_media_client_closes_writer_when_drain_is_cancelled(monkeypatch: Any) -> None:
    """Verify cancellation during request write still closes the socket writer."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = SlowWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a writer that stalls while draining."""
        return FakeResponseReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy)
    task = asyncio.create_task(client.fetch("https://pbs.twimg.com/media/1.jpg"))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_closes_writer_when_fetch_is_cancelled(monkeypatch: Any) -> None:
    """Verify cancellation during response headers does not leak the socket writer."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a slow header reader and tracked writer."""
        return SlowHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy)
    task = asyncio.create_task(client.fetch("https://pbs.twimg.com/media/1.jpg"))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_maps_header_timeout_to_safe_media_error(monkeypatch: Any) -> None:
    """Verify a stalled upstream header read returns the stable media error."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a reader that exceeds the configured header timeout."""
        return SlowHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy, read_timeout=0.01)

    with pytest.raises(AppError) as exc_info:
        await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_client_maps_header_transport_error_to_safe_media_error(
    monkeypatch: Any,
) -> None:
    """Verify header transport errors do not escape as generic application failures."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a reader that fails while parsing response headers."""
        return FailingHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize("reader_type", [IncompleteHeaderReader, LimitHeaderReader])
@pytest.mark.asyncio
async def test_media_client_maps_header_parse_errors_to_safe_media_error(
    monkeypatch: Any,
    reader_type: type[FakeResponseReader],
) -> None:
    """Verify malformed or truncated headers never escape as generic failures."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a reader that fails while parsing response headers."""
        return reader_type(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"
    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_bounds_failed_writer_cleanup(monkeypatch: Any) -> None:
    """Verify timeout cleanup cannot block longer than the media request."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = SlowCloseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a reader that times out with a slow-closing writer."""
        return SlowHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    with pytest.raises(AppError) as exc_info:
        await asyncio.wait_for(
            MediaClient(policy, read_timeout=0.01).fetch("https://pbs.twimg.com/media/1.jpg"),
            timeout=0.1,
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_detaches_cancellation_resistant_writer_cleanup() -> None:
    """writer cleanup timeout 後應觀察 detached task 的最終例外。"""
    writer = CancellationResistantCloseWriter()
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    client = MediaClient(policy, read_timeout=0.01)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 cleanup observer 消耗的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        cleanup_task = asyncio.create_task(client._close_writer(writer))
        await writer.close_started.wait()
        await asyncio.wait_for(cleanup_task, timeout=0.1)
        await asyncio.wait_for(writer.close_cancelled.wait(), timeout=0.1)
        writer.release_close.set()
        await asyncio.wait_for(writer.close_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        writer.release_close.set()
        if not writer.close_finished.is_set():
            await asyncio.wait_for(writer.close_finished.wait(), timeout=0.1)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_media_client_total_deadline_covers_header_read(monkeypatch: Any) -> None:
    """Verify the total media deadline starts before upstream headers are read."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a reader that exceeds the complete request deadline."""
        return SlowHeaderReader(b""), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy, read_timeout=1.0, total_timeout=0.01)

    with pytest.raises(AppError) as exc_info:
        await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_client_preserves_total_deadline_after_headers(monkeypatch: Any) -> None:
    """Verify body streaming retains the original total deadline after headers arrive."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a complete response whose body can be streamed after headers."""
        return (
            FakeResponseReader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
            writer,
        )

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    started = time.monotonic()
    response = await MediaClient(
        policy,
        read_timeout=30.0,
        total_timeout=120.0,
    ).fetch("https://pbs.twimg.com/media/1.jpg")

    assert response._deadline is not None
    assert response._deadline - started > 60.0
    await response.close()


@pytest.mark.asyncio
async def test_media_client_maps_transport_error_to_safe_media_error(monkeypatch: Any) -> None:
    """Verify a refused upstream connection does not escape as an HTTP 500."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    async def refused_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Raise the transport error produced by a refused CDN connection."""
        raise ConnectionRefusedError("private socket detail")

    monkeypatch.setattr(asyncio, "open_connection", refused_connection)

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_client_total_deadline_covers_dns_resolution(
    monkeypatch: Any,
) -> None:
    """Verify a blocking resolver cannot outlive the complete media deadline."""

    def slow_resolver(
        _host: str, _port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Simulate a DNS lookup slower than the configured request deadline."""
        time.sleep(0.05)
        return [ipaddress.ip_address("93.184.216.34")]

    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=slow_resolver,
    )

    async def unexpected_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Fail the test if the request continues after DNS deadline expiry."""
        raise AssertionError("connection should not start after DNS timeout")

    monkeypatch.setattr(asyncio, "open_connection", unexpected_connection)

    started = time.monotonic()
    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy, total_timeout=0.001).fetch("https://pbs.twimg.com/media/1.jpg")

    assert exc_info.value.code == "upstream_media_invalid"
    assert time.monotonic() - started < 0.04


@pytest.mark.asyncio
async def test_media_client_closes_writer_when_deadline_expires_after_headers(
    monkeypatch: Any,
) -> None:
    """Verify a deadline failure after header parsing still closes the writer."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    writer = FakeResponseWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """Return a complete header response with tracked cleanup."""
        return (
            FakeResponseReader(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n\r\n"),
            writer,
        )

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy)
    calls = 0

    def deadline_after_headers(_deadline: float, _maximum: float) -> float:
        """Expire only when the response-body deadline is initialized."""
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise AppError("upstream_media_invalid", "deadline expired")
        return 1.0

    monkeypatch.setattr(client, "_remaining_timeout", deadline_after_headers)

    with pytest.raises(AppError, match="deadline expired"):
        await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert writer.closed is True


@pytest.mark.asyncio
async def test_media_client_pins_ip_and_preserves_tls_hostname(monkeypatch: Any) -> None:
    """Verify direct transport uses selected IP and original TLS hostname."""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"pbs.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    captured: dict[str, Any] = {}

    async def fake_open_connection(
        *args: Any, **kwargs: Any
    ) -> tuple[FakeResponseReader, FakeResponseWriter]:
        """Capture socket and TLS arguments and return a response."""
        captured["args"] = args
        captured["kwargs"] = kwargs
        writer = FakeResponseWriter()
        captured["writer"] = writer
        return (
            FakeResponseReader(
                b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\n", b"abc"
            ),
            writer,
        )

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy, max_redirects=2)

    response = await client.fetch("https://pbs.twimg.com/media/1.jpg?name=orig")

    assert response.status_code == 200
    assert captured["args"][:2] == ("93.184.216.34", 443)
    assert captured["kwargs"]["server_hostname"] == "pbs.twimg.com"
    assert b"Host: pbs.twimg.com\r\n" in captured["writer"].writes[0]


@pytest.mark.asyncio
async def test_media_client_builds_internal_range_header(monkeypatch: Any) -> None:
    """MediaClient 應只由內部 offset 建立 Range header。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"video.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )
    captured: dict[str, Any] = {}

    async def fake_fetch_target(
        _target: Any,
        headers: dict[str, str],
        *,
        deadline: float | None,
    ) -> MediaResponse:
        """捕捉 normalized headers 並回傳受控 partial response。"""
        captured["headers"] = dict(headers)
        captured["deadline"] = deadline
        return MediaResponse(
            206,
            {
                "content-type": "video/mp4",
                "content-range": "bytes 4-9/10",
                "content-length": "6",
            },
            FakeResponseReader(b"efghij"),
            FakeResponseWriter(),
            max_bytes=100,
        )

    client = MediaClient(policy)
    monkeypatch.setattr(client, "_fetch_target", fake_fetch_target)

    response = await client.fetch(
        "https://video.twimg.com/media/1.mp4",
        headers={"User-Agent": "safe"},
        range_start=4,
    )

    assert response.status_code == 206
    assert captured["headers"] == {"User-Agent": "safe", "Range": "bytes=4-"}
    assert captured["deadline"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("header_name", ["Range", "range", "If-Range", "Authorization"])
async def test_media_client_rejects_external_range_or_sensitive_header_injection(
    header_name: str,
) -> None:
    """caller 不得注入 Range、If-Range 或 credentials header。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"video.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch(
            "https://video.twimg.com/media/1.mp4",
            headers={header_name: "bytes=0-"},
            range_start=4,
        )

    assert exc_info.value.code == "extraction_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("range_start", [-1, True, 1.5], ids=["negative", "boolean", "float"])
async def test_media_client_rejects_invalid_internal_range_offset(range_start: Any) -> None:
    """MediaClient 應拒絕非負整數以外的 internal Range offset。"""
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset({"video.twimg.com"}),
        allowed_suffixes=frozenset(),
        resolver=public_resolver,
    )

    with pytest.raises(AppError) as exc_info:
        await MediaClient(policy).fetch(
            "https://video.twimg.com/media/1.mp4",
            range_start=range_start,
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_client_revalidates_redirect_destination_for_range_request(
    monkeypatch: Any,
) -> None:
    """Range request 的每一跳 redirect 都應重新驗證並保留內部 header。"""
    addresses = {
        "video.twimg.com": ipaddress.ip_address("93.184.216.34"),
        "pbs.twimg.com": ipaddress.ip_address("93.184.216.35"),
    }
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset(addresses),
        allowed_suffixes=frozenset(),
        resolver=lambda host, _port: [addresses[host]],
    )
    responses = [
        FakeResponseReader(
            b"HTTP/1.1 302 Found\r\nLocation: https://pbs.twimg.com/media/1.mp4\r\n\r\n"
        ),
        FakeResponseReader(
            b"HTTP/1.1 206 Partial Content\r\n"
            b"Content-Type: video/mp4\r\n"
            b"Content-Range: bytes 4-9/10\r\n"
            b"Content-Length: 6\r\n\r\n",
            b"efghij",
        ),
    ]
    writers: list[FakeResponseWriter] = []

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳 redirect 與 partial response，並保存每次 request writer。"""
        writer = FakeResponseWriter()
        writers.append(writer)
        return responses.pop(0), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    response = await MediaClient(policy).fetch(
        "https://video.twimg.com/media/1.mp4",
        range_start=4,
    )

    assert response.status_code == 206
    assert len(writers) == 2
    assert all(b"Range: bytes=4-\r\n" in writer.writes[0] for writer in writers)


def test_resume_accepts_matching_206_content_range() -> None:
    """resume validator 應接受 offset、total、MIME 與長度都相符的 response。"""
    response = MediaResponse(
        206,
        {
            "content-type": "video/mp4 ; codecs=avc1",
            "content-range": "bytes 4-9/10",
            "content-length": "6",
        },
        FakeResponseReader(b"efghij"),
        FakeResponseWriter(),
        max_bytes=100,
    )

    parsed = validate_resume_response(
        response,
        requested_offset=4,
        original_total_length=10,
        original_content_type="video/mp4",
    )

    assert parsed == ParsedContentRange(start=4, end=9, total=10)


@pytest.mark.parametrize(
    ("status_code", "content_range", "content_length", "content_type"),
    [
        (200, "bytes 4-9/10", "6", "video/mp4"),
        (206, None, "6", "video/mp4"),
        (206, "bytes 5-9/10", "5", "video/mp4"),
        (206, "bytes 4-8/10", "5", "video/mp4"),
        (206, "bytes 4-9/11", "6", "video/mp4"),
        (206, "bytes 4-9/10", "5", "video/mp4"),
        (206, "bytes 4-9/10", "6", "video/webm"),
        (206, "bytes 010-9/10", "0", "video/mp4"),
    ],
    ids=[
        "status-200",
        "missing-content-range",
        "wrong-start",
        "wrong-end",
        "wrong-total",
        "content-length-mismatch",
        "mime-mismatch",
        "invalid-content-range",
    ],
)
def test_resume_rejects_invalid_response_proof(
    status_code: int,
    content_range: str | None,
    content_length: str | None,
    content_type: str,
) -> None:
    """resume validator 應在 body 拼接前拒絕所有不相符 response proof。"""
    headers = {"content-type": content_type}
    if content_range is not None:
        headers["content-range"] = content_range
    if content_length is not None:
        headers["content-length"] = content_length
    response = MediaResponse(
        status_code,
        headers,
        FakeResponseReader(b"efghij"),
        FakeResponseWriter(),
        max_bytes=100,
    )

    with pytest.raises(AppError) as exc_info:
        validate_resume_response(
            response,
            requested_offset=4,
            original_total_length=10,
            original_content_type="video/mp4",
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_media_client_revalidates_redirect_destination(monkeypatch: Any) -> None:
    """Verify each redirect opens a new validated destination connection."""
    addresses = {
        "pbs.twimg.com": ipaddress.ip_address("93.184.216.34"),
        "video.twimg.com": ipaddress.ip_address("93.184.216.35"),
    }
    policy = MediaDestinationPolicy(
        allowed_exact_hosts=frozenset(addresses),
        allowed_suffixes=frozenset(),
        resolver=lambda host, _port: [addresses[host]],
    )
    calls: list[tuple[str, str]] = []
    responses = [
        FakeResponseReader(
            b"HTTP/1.1 302 Found\r\nLocation: https://video.twimg.com/x.mp4\r\n\r\n"
        ),
        FakeResponseReader(
            b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 0\r\n\r\n"
        ),
    ]

    async def fake_open_connection(
        *args: Any, **kwargs: Any
    ) -> tuple[FakeResponseReader, FakeResponseWriter]:
        """Capture each redirect connection and return its response."""
        calls.append((args[0], kwargs["server_hostname"]))
        return responses.pop(0), FakeResponseWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    client = MediaClient(policy, max_redirects=2)

    response = await client.fetch("https://pbs.twimg.com/media/1.jpg")

    assert response.status_code == 200
    assert calls == [("93.184.216.34", "pbs.twimg.com"), ("93.184.216.35", "video.twimg.com")]
