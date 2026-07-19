"""Loopback HTTP CONNECT proxy used to restrict extractor egress."""

import asyncio

from ..errors import AppError
from .dns import DestinationPolicy


def parse_connect_request(request: bytes) -> tuple[str, int]:
    """Parse a CONNECT request and accept only hostname:443 targets."""
    try:
        request_line = request.split(b"\r\n", 1)[0].decode("ascii")
        method, target, version = request_line.split(" ", 2)
        hostname, port_text = target.rsplit(":", 1)
        port = int(port_text)
    except (UnicodeDecodeError, ValueError) as error:
        raise AppError("unsafe_destination", "Only HTTPS CONNECT targets are allowed.") from error

    if method != "CONNECT" or version != "HTTP/1.1" or not hostname or port != 443:
        raise AppError("unsafe_destination", "Only HTTPS CONNECT targets are allowed.")
    if any(character in hostname for character in "[]/\\?#@ "):
        raise AppError("unsafe_destination", "The CONNECT hostname is invalid.")
    return hostname.lower().rstrip("."), port


class ConnectProxy:
    """Tunnel extractor CONNECT requests only to policy-approved destinations."""

    def __init__(self, policy: DestinationPolicy, *, max_header_bytes: int = 8192) -> None:
        """Create a proxy with a destination policy and bounded request headers."""
        self.policy = policy
        self.max_header_bytes = max_header_bytes

    async def serve(self, host: str, port: int) -> asyncio.AbstractServer:
        """Start the loopback proxy server and return its server handle."""
        return await asyncio.start_server(self.handle_client, host, port)

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Validate one CONNECT request and relay its encrypted tunnel."""
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if len(request) > self.max_header_bytes:
                raise AppError("unsafe_destination", "The CONNECT request is too large.")
            hostname, port = parse_connect_request(request)
            target = self.policy.validate(hostname, port)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                str(target.address), target.port
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _relay(reader, writer, upstream_reader, upstream_writer)
        except (AppError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy bytes in both directions until either side closes."""
    await asyncio.gather(
        _copy_stream(client_reader, upstream_writer),
        _copy_stream(upstream_reader, client_writer),
    )


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Copy bounded chunks from one stream to another."""
    while chunk := await reader.read(64 * 1024):
        writer.write(chunk)
        await writer.drain()
