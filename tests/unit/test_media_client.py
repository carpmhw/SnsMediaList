"""Tests for safe media destination and response validation."""

import asyncio
import ipaddress
from typing import Any

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.media_client import (
    MediaClient,
    MediaDestinationPolicy,
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

    def write(self, value: bytes) -> None:
        """Capture bytes written to the fake upstream."""
        self.writes.append(value)

    async def drain(self) -> None:
        """Complete a fake write."""

    def close(self) -> None:
        """Close the fake upstream connection."""

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


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
