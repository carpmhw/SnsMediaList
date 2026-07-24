"""Safe CDN URL, TLS target, response header, and preview validation."""

from __future__ import annotations

import asyncio
import ssl
import time
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
        total_timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        """Store response metadata and bounded streaming state."""
        self.status_code = status_code
        self.headers = headers
        self._reader = reader
        self._writer = writer
        self._max_bytes = max_bytes
        self._read_timeout = read_timeout
        self._deadline: float | None
        if deadline is not None:
            self._deadline = deadline
        elif total_timeout is None:
            self._deadline = None
        else:
            self._deadline = time.monotonic() + total_timeout
        self._bytes_read = 0

    async def iter_bytes(
        self, chunk_size: int = 64 * 1024, *, max_bytes: int | None = None
    ) -> AsyncIterator[bytes]:
        """Yield response body chunks while enforcing the configured or caller limit."""
        limit = self._max_bytes if max_bytes is None else min(self._max_bytes, max_bytes)
        content_length = self._content_length()
        if content_length is not None and content_length > limit:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")

        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            async for chunk in self._iter_chunked(limit=limit, chunk_size=chunk_size):
                yield chunk
            return

        remaining = content_length
        bytes_read = 0
        while remaining is None or remaining > 0:
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = await self._read(size)
            if not chunk:
                if remaining is not None and remaining > 0:
                    raise AppError(
                        "upstream_media_invalid", "The media response body was truncated."
                    )
                break
            bytes_read += len(chunk)
            if bytes_read > limit:
                raise AppError(
                    "upstream_media_invalid", "The media response exceeds its size limit."
                )
            self._record_bytes(len(chunk))
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk

    async def close(self) -> None:
        """Close the upstream connection and release its resources."""
        self._writer.close()
        try:
            timeout = self._remaining_timeout()
            await asyncio.wait_for(self._writer.wait_closed(), timeout=timeout)
        except (AppError, TimeoutError, OSError):
            return
        except RuntimeError as error:
            if "handler is closed" not in str(error):
                raise

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

    async def _iter_chunked(self, *, limit: int, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield chunked response bytes while enforcing a caller-provided limit."""
        bytes_read = 0
        while True:
            line = await self._read_until(b"\r\n")
            try:
                size = int(line.strip().split(b";", 1)[0], 16)
            except ValueError as error:
                raise AppError(
                    "upstream_media_invalid", "The media response chunks are invalid."
                ) from error
            if size < 0:
                raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
            if size == 0:
                await self._read_until(b"\r\n")
                return
            if bytes_read + size > limit:
                raise AppError(
                    "upstream_media_invalid", "The media response exceeds its size limit."
                )
            remaining_chunk = size
            while remaining_chunk > 0:
                read_size = min(remaining_chunk, chunk_size)
                chunk = await self._read_exactly(read_size)
                bytes_read += len(chunk)
                if bytes_read > limit:
                    raise AppError(
                        "upstream_media_invalid", "The media response exceeds its size limit."
                    )
                self._record_bytes(len(chunk))
                yield chunk
                remaining_chunk -= len(chunk)
            if await self._read_exactly(2) != b"\r\n":
                raise AppError("upstream_media_invalid", "The media response chunks are invalid.")

    def _record_bytes(self, count: int) -> None:
        """Record body bytes and reject responses exceeding the configured limit."""
        self._bytes_read += count
        if self._bytes_read > self._max_bytes:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")

    async def _read(self, size: int) -> bytes:
        """Read a body chunk before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(self._reader.read(size), self._remaining_timeout())
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except OSError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error

    async def _read_until(self, separator: bytes) -> bytes:
        """Read a protocol line before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(
                self._reader.readuntil(separator), self._remaining_timeout()
            )
        except asyncio.IncompleteReadError as error:
            raise AppError("upstream_media_invalid", "The media response was truncated.") from error
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except (asyncio.LimitOverrunError, OSError) as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error

    async def _read_exactly(self, size: int) -> bytes:
        """Read an exact protocol section before the configured upstream deadline."""
        try:
            return await asyncio.wait_for(self._reader.readexactly(size), self._remaining_timeout())
        except asyncio.IncompleteReadError as error:
            raise AppError("upstream_media_invalid", "The media response was truncated.") from error
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except OSError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error

    def _remaining_timeout(self) -> float:
        """Return the smaller per-read or total-response timeout remaining."""
        if self._deadline is None:
            return self._read_timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("upstream_media_invalid", "The media response timed out.")
        return min(self._read_timeout, remaining)


class MediaClient:
    """Fetch approved CDN resources with pinned TLS connections and redirects."""

    def __init__(
        self,
        policy: MediaDestinationPolicy,
        *,
        max_redirects: int = 3,
        connect_timeout: float = 10.0,
        max_bytes: int = 500_000_000,
        read_timeout: float = 30.0,
        total_timeout: float = 120.0,
    ) -> None:
        """Store destination and resource limits for outbound requests."""
        self.policy = policy
        self.max_redirects = max_redirects
        self.connect_timeout = connect_timeout
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout

    async def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
    ) -> MediaResponse:
        """Fetch a URL and manually revalidate every approved redirect."""
        current_url = url
        request_headers = normalize_request_headers(headers or {})
        deadline = time.monotonic() + self.total_timeout
        for redirect_count in range(self.max_redirects + 1):
            target = await self._validate_target(current_url, deadline=deadline)
            try:
                response = await self._fetch_target(target, request_headers, deadline=deadline)
            except AppError:
                raise
            except asyncio.CancelledError:
                raise
            except OSError as error:
                raise AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                ) from error
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

    async def _validate_target(
        self,
        url: str,
        *,
        deadline: float,
    ) -> ValidatedMediaTarget:
        """Validate and resolve one media target without blocking the event loop."""
        timeout = self._remaining_timeout(deadline, self.read_timeout)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.policy.validate_url, url),
                timeout=timeout,
            )
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error

    async def _fetch_target(
        self,
        target: ValidatedMediaTarget,
        headers: Mapping[str, str],
        *,
        deadline: float,
    ) -> MediaResponse:
        """Open one pinned TLS connection and parse its response headers."""
        context = ssl.create_default_context()
        try:
            connect_timeout = self._remaining_timeout(deadline, self.connect_timeout)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    str(target.address),
                    target.port,
                    ssl=context,
                    server_hostname=target.hostname,
                ),
                timeout=connect_timeout,
            )
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        path = target.path
        if target.query:
            path = f"{path}?{target.query}"
        request_lines = [f"GET {path} HTTP/1.1", f"Host: {target.hostname}", "Connection: close"]
        request_lines.extend(f"{name}: {value}" for name, value in headers.items())
        try:
            writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
            write_timeout = self._remaining_timeout(deadline, self.connect_timeout)
            await asyncio.wait_for(writer.drain(), timeout=write_timeout)
            header_timeout = self._remaining_timeout(deadline, self.read_timeout)
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=header_timeout,
            )
            status_code, response_headers = _parse_response_headers(raw_headers)
        except TimeoutError as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media headers were invalid."
            ) from error
        except BaseException:
            await self._close_writer(writer)
            raise
        try:
            self._remaining_timeout(deadline, self.read_timeout)
        except AppError:
            await self._close_writer(writer)
            raise
        return MediaResponse(
            status_code,
            response_headers,
            reader,
            writer,
            max_bytes=self.max_bytes,
            read_timeout=self.read_timeout,
            deadline=deadline,
        )

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        """Close a failed request writer without masking its primary error."""
        writer.close()
        task = asyncio.create_task(writer.wait_closed())
        done, _pending = await asyncio.wait({task}, timeout=min(self.read_timeout, 1.0))
        if done:
            await asyncio.gather(task, return_exceptions=True)
        else:
            task.cancel()

    def _remaining_timeout(self, deadline: float, maximum: float) -> float:
        """Return the smaller per-operation timeout and remaining request deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("upstream_media_invalid", "The upstream media request timed out.")
        return min(maximum, remaining)


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
    expected_media_class: str | None = None,
) -> AsyncIterator[bytes]:
    """Validate response status/MIME/class and optionally a raster signature before yielding."""
    if not 200 <= response.status_code < 300:
        raise AppError("upstream_media_invalid", "The upstream media response was not successful.")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if expected_media_class == "image" and not content_type.startswith("image/"):
        raise AppError("upstream_media_invalid", "The media content type is not supported.")
    if expected_media_class == "video" and not content_type.startswith("video/"):
        raise AppError("upstream_media_invalid", "The media content type is not supported.")
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
    async for chunk in response.iter_bytes(chunk_size=512 if preview else 64 * 1024):
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
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
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
    return headers


@dataclass(frozen=True, slots=True)
class MediaDestinationPolicy:
    """Validate exact or boundary-safe CDN host allowlists and DNS answers."""

    allowed_exact_hosts: frozenset[str]
    allowed_suffixes: frozenset[str]
    resolver: Resolver

    def validate_url(self, url: str) -> ValidatedMediaTarget:
        """Validate a clean HTTPS CDN URL and pin all DNS answers."""
        if any(ord(char) < 0x20 or ord(char) == 0x7F or ord(char) > 0x7F for char in url):
            raise AppError("unsafe_destination", "The media destination is invalid.")
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
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self'; style-src 'none'; script-src 'none'"
        ),
    }
