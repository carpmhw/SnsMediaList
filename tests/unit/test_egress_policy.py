"""Tests for extractor egress destination policy and CONNECT parsing."""

import ipaddress
from typing import Any

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.connect_proxy import parse_connect_request
from sns_media_list.network.dns import DestinationPolicy


class FakeStreamReader:
    """Provide the minimal async reader surface used by the proxy test."""

    def __init__(self, request: bytes = b"") -> None:
        """Store one request and then return EOF for relay reads."""
        self.request = request

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return the configured request headers."""
        return self.request

    async def read(self, _size: int) -> bytes:
        """Return EOF so the fake tunnel closes immediately."""
        return b""


class FakeStreamWriter:
    """Provide the minimal async writer surface used by the proxy test."""

    def __init__(self) -> None:
        """Initialize captured writes and close state."""
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        """Capture bytes written by the proxy."""
        self.writes.append(value)

    async def drain(self) -> None:
        """Complete a fake write without blocking."""

    def close(self) -> None:
        """Close the fake stream."""

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


def test_policy_selects_a_validated_public_address() -> None:
    """Verify a permitted host is pinned to one validated resolver result."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )

    target = policy.validate("www.instagram.com", 443)

    assert target.hostname == "www.instagram.com"
    assert str(target.address) == "93.184.216.34"


def test_policy_rejects_deceptive_host() -> None:
    """Verify an allowed hostname suffix cannot be faked by another host."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate("www.instagram.com.evil.example", 443)

    assert exc_info.value.code == "unsafe_destination"


def test_cross_host_redirect_requires_a_new_allowlist_check() -> None:
    """Verify a redirect target cannot reuse the source host authorization."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    policy.validate("www.instagram.com", 443)

    with pytest.raises(AppError) as exc_info:
        policy.validate("video.external.example", 443)

    assert exc_info.value.code == "unsafe_destination"


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "::1", "fc00::1", "2001:db8::1"]
)
def test_policy_rejects_any_non_public_dns_answer(address: str) -> None:
    """Verify one unsafe A or AAAA answer rejects the complete destination."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address(address),
        ],
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate("www.instagram.com", 443)

    assert exc_info.value.code == "unsafe_destination"


def test_connect_request_requires_https_port() -> None:
    """Verify only CONNECT host:443 requests are accepted by the proxy parser."""
    host, port = parse_connect_request(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )

    assert host == "www.instagram.com"
    assert port == 443


@pytest.mark.asyncio
async def test_proxy_connects_to_selected_pinned_address(monkeypatch: Any) -> None:
    """Verify the proxy connects to the validated IP rather than re-resolving."""
    from sns_media_list.network import connect_proxy

    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = connect_proxy.ConnectProxy(policy)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = FakeStreamWriter()
    captured: dict[str, object] = {}

    async def fake_open_connection(
        host: str, port: int
    ) -> tuple[FakeStreamReader, FakeStreamWriter]:
        """Capture the pinned destination requested by the proxy."""
        captured.update({"host": host, "port": port})
        return FakeStreamReader(), FakeStreamWriter()

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)

    await proxy.handle_client(client, client_writer)

    assert captured == {"host": "93.184.216.34", "port": 443}
    assert client_writer.writes[0].startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize(
    "raw_request",
    [
        b"GET https://www.instagram.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT www.instagram.com:80 HTTP/1.1\r\n\r\n",
    ],
)
def test_connect_request_rejects_unsafe_forms(raw_request: bytes) -> None:
    """Verify malformed and unsafe CONNECT requests fail before networking."""
    with pytest.raises(AppError) as exc_info:
        parse_connect_request(raw_request)

    assert exc_info.value.code == "unsafe_destination"
