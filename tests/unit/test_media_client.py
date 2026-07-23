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
    build_preview_headers,
    connection_target,
    validate_preview_signature,
)


def public_resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return a deterministic public address for destination tests."""
    return [ipaddress.ip_address("93.184.216.34")]


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
    assert headers["Cache-Control"] == "private, no-store"
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


class SlowHeaderReader(FakeResponseReader):
    """Delay response headers so cancellation occurs during header parsing."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Wait until the transport request is cancelled."""
        await asyncio.sleep(1)
        return await super().readuntil(_separator)


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


class FailingCloseWriter(FakeResponseWriter):
    """Raise a socket cleanup error after the writer is closed."""

    async def wait_closed(self) -> None:
        """Raise a transport error that cleanup should safely absorb."""
        raise OSError("private socket cleanup detail")


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
