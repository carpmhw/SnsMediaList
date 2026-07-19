"""Safe CDN URL, TLS target, response header, and preview validation."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

from ..errors import AppError
from ..extractor.normalizer import normalize_request_headers, sanitize_filename
from .dns import IPAddress, Resolver

_PREVIEW_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


@dataclass(frozen=True, slots=True)
class ValidatedMediaTarget:
    """Represent a CDN URL pinned to a validated public address."""

    url: str
    hostname: str
    port: int
    address: IPAddress
    path: str
    query: str


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """Describe the socket address and original hostname for TLS/HTTP."""

    address: str
    port: int
    server_hostname: str
    host_header: str


class MediaResponse:
    """Represent a streaming HTTP response and its open upstream connection."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_bytes: int,
        read_timeout: float = 30.0,
    ) -> None:
        """Store response metadata and bounded streaming state."""
        self.status_code = status_code
        self.headers = headers
        self._reader = reader
        self._writer = writer
        self._max_bytes = max_bytes
        self._read_timeout = read_timeout
        self._bytes_read = 0

    async def iter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield response body chunks while enforcing the byte limit."""
        content_length = self._content_length()
        if content_length is not None and content_length > self._max_bytes:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")

        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            async for chunk in self._iter_chunked():
                yield chunk
            return

        remaining = content_length
        while remaining is None or remaining > 0:
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = await self._read(size)
            if not chunk:
                break
            self._record_bytes(len(chunk))
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk

    async def close(self) -> None:
        """Close the upstream connection and release its resources."""
        self._writer.close()
        await self._writer.wait_closed()

    def _content_length(self) -> int | None:
        """Parse a valid optional Content-Length response header."""
        value = self.headers.get("content-length")
        if value is None:
            return None
        try:
            length = int(value)
        except ValueError as error:
            raise AppError(
                "upstream_media_invalid", "The media response length is invalid."
            ) from error
        if length < 0:
            raise AppError("upstream_media_invalid", "The media response length is invalid.")
        return length

    async def _iter_chunked(self) -> AsyncIterator[bytes]:
        """Yield chunks from an HTTP chunked response."""
        while True:
            line = await self._read_until(b"\r\n")
            try:
                size = int(line.strip().split(b";", 1)[0], 16)
            except ValueError as error:
                raise AppError(
                    "upstream_media_invalid", "The media response chunks are invalid."
                ) from error
            if size == 0:
                await self._read_until(b"\r\n")
                return
            chunk = await self._read_exactly(size)
            await self._read_exactly(2)
            self._record_bytes(len(chunk))
            yield chunk

    def _record_bytes(self, count: int) -> None:
        """Record body bytes and reject responses exceeding the configured limit."""
        self._bytes_read += count
        if self._bytes_read > self._max_bytes:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")

    async def _read(self, size: int) -> bytes:
        """Read a body chunk before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(self._reader.read(size), self._read_timeout)
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error

    async def _read_until(self, separator: bytes) -> bytes:
        """Read a protocol line before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(self._reader.readuntil(separator), self._read_timeout)
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error

    async def _read_exactly(self, size: int) -> bytes:
        """Read an exact protocol section before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(self._reader.readexactly(size), self._read_timeout)
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error


class MediaClient:
    """Fetch approved CDN resources with pinned TLS connections and redirects."""

    def __init__(
        self,
        policy: MediaDestinationPolicy,
        *,
        max_redirects: int = 3,
        connect_timeout: float = 10.0,
        max_bytes: int = 500_000_000,
    ) -> None:
        """Store destination and resource limits for outbound requests."""
        self.policy = policy
        self.max_redirects = max_redirects
        self.connect_timeout = connect_timeout
        self.max_bytes = max_bytes

    async def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
    ) -> MediaResponse:
        """Fetch a URL and manually revalidate every approved redirect."""
        current_url = url
        request_headers = normalize_request_headers(headers or {})
        for redirect_count in range(self.max_redirects + 1):
            target = self.policy.validate_url(current_url)
            response = await self._fetch_target(target, request_headers)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            await response.close()
            if not location or redirect_count >= self.max_redirects:
                raise AppError(
                    "unsafe_destination", "The media response redirected too many times."
                )
            current_url = urljoin(current_url, location)
        raise AppError("unsafe_destination", "The media response redirected too many times.")

    async def _fetch_target(
        self,
        target: ValidatedMediaTarget,
        headers: Mapping[str, str],
    ) -> MediaResponse:
        """Open one pinned TLS connection and parse its response headers."""
        context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                str(target.address),
                target.port,
                ssl=context,
                server_hostname=target.hostname,
            ),
            timeout=self.connect_timeout,
        )
        path = target.path
        if target.query:
            path = f"{path}?{target.query}"
        request_lines = [f"GET {path} HTTP/1.1", f"Host: {target.hostname}", "Connection: close"]
        request_lines.extend(f"{name}: {value}" for name, value in headers.items())
        writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        try:
            raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30.0)
            status_code, response_headers = _parse_response_headers(raw_headers)
        except Exception:
            writer.close()
            await writer.wait_closed()
            raise
        return MediaResponse(
            status_code,
            response_headers,
            reader,
            writer,
            max_bytes=self.max_bytes,
        )


def _parse_response_headers(raw_headers: bytes) -> tuple[int, dict[str, str]]:
    """Parse a bounded HTTP status line and lowercase response headers."""
    try:
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        version, status_text, *_ = lines[0].split(" ", 2)
        status_code = int(status_text)
        if version != "HTTP/1.1":
            raise ValueError("unsupported HTTP version")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
        return status_code, headers
    except (UnicodeDecodeError, ValueError) as error:
        raise AppError(
            "upstream_media_invalid", "The media response headers are invalid."
        ) from error


async def iter_validated_body(
    response: MediaResponse,
    *,
    preview: bool = False,
) -> AsyncIterator[bytes]:
    """Validate response status/MIME and optionally a raster signature before yielding."""
    if not 200 <= response.status_code < 300:
        raise AppError("upstream_media_invalid", "The upstream media response was not successful.")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if preview:
        if content_type not in _PREVIEW_SIGNATURES:
            raise AppError("upstream_media_invalid", "The preview content type is not supported.")
    elif not (content_type.startswith("image/") or content_type.startswith("video/")):
        raise AppError("upstream_media_invalid", "The media content type is not supported.")

    if not preview:
        async for chunk in response.iter_bytes():
            yield chunk
        return

    buffered = bytearray()
    validated = False
    async for chunk in response.iter_bytes():
        if not validated:
            buffered.extend(chunk)
            if len(buffered) < 512:
                continue
            prefix = bytes(buffered[:512])
            if not validate_preview_signature(content_type, prefix):
                raise AppError("upstream_media_invalid", "The preview signature is invalid.")
            validated = True
            yield bytes(buffered)
            buffered.clear()
            continue
        yield chunk

    if not validated:
        if not validate_preview_signature(content_type, bytes(buffered)):
            raise AppError("upstream_media_invalid", "The preview signature is invalid.")
        yield bytes(buffered)


def build_download_headers(filename: str) -> dict[str, str]:
    """Build safe attachment response headers for a media download."""
    safe_name = quote(sanitize_filename(filename), safe="")
    return {
        "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
        "X-Content-Type-Options": "nosniff",
    }


def build_forward_response_headers(
    response: MediaResponse,
    *,
    filename: str,
    preview: bool,
) -> dict[str, str]:
    """Forward only validated media headers and safe disposition controls."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    headers = build_preview_headers(filename) if preview else build_download_headers(filename)
    headers["Content-Type"] = content_type
    content_length = response.headers.get("content-length")
    if content_length is not None:
        headers["Content-Length"] = content_length
    return headers


@dataclass(frozen=True, slots=True)
class MediaDestinationPolicy:
    """Validate exact or boundary-safe CDN host allowlists and DNS answers."""

    allowed_exact_hosts: frozenset[str]
    allowed_suffixes: frozenset[str]
    resolver: Resolver

    def validate_url(self, url: str) -> ValidatedMediaTarget:
        """Validate a clean HTTPS CDN URL and pin all DNS answers."""
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise AppError("unsafe_destination", "The media destination is invalid.") from error

        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port not in (None, 443)
            or not self._allowed_host(hostname)
        ):
            raise AppError("unsafe_destination", "The media destination is not allowed.")

        addresses = list(self.resolver(hostname, 443))
        if not addresses or any(not address.is_global for address in addresses):
            raise AppError("unsafe_destination", "The media destination is not public.")
        return ValidatedMediaTarget(
            url=url,
            hostname=hostname,
            port=443,
            address=addresses[0],
            path=parsed.path or "/",
            query=parsed.query,
        )

    def _allowed_host(self, hostname: str) -> bool:
        """Check exact hosts and suffixes without accepting deceptive domains."""
        return hostname in self.allowed_exact_hosts or any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in self.allowed_suffixes
        )


def connection_target(target: ValidatedMediaTarget) -> ConnectionTarget:
    """Build pinned socket and original-host TLS/HTTP parameters."""
    return ConnectionTarget(
        address=str(target.address),
        port=target.port,
        server_hostname=target.hostname,
        host_header=target.hostname,
    )


def validate_preview_signature(content_type: str, prefix: bytes) -> bool:
    """Validate an allowlisted raster MIME type against its file signature."""
    signatures = _PREVIEW_SIGNATURES.get(content_type.lower())
    if not signatures:
        return False
    if content_type.lower() == "image/webp":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    return any(prefix.startswith(signature) for signature in signatures)


def build_preview_headers(filename: str) -> dict[str, str]:
    """Build passive inline preview response headers."""
    safe_name = quote(sanitize_filename(filename), safe="")
    return {
        "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self'; style-src 'none'; script-src 'none'"
        ),
    }
