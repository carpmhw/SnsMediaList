"""Safe CDN URL, TLS target, response header, and preview validation."""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
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
_HEADER_TOKEN_EXTRA = frozenset("!#$%&'*+-.^_`|~")
_MAX_CHUNK_TRAILER_BYTES = 8 * 1024
_DETACHED_CONNECTION_CLEANUP_TIMEOUT_SECONDS = 1.0
_FORBIDDEN_TRAILER_FIELDS = frozenset(
    {
        "content-length",
        "transfer-encoding",
        "host",
        "connection",
        "te",
        "trailer",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "keep-alive",
        "authorization",
        "cache-control",
        "content-type",
        "content-encoding",
        "content-range",
    }
)


class _BoundedOperationTimeout(Exception):
    """表示由 bounded operation helper 自己觸發的 timeout。"""


async def _await_bounded[T](
    awaitable: Awaitable[T],
    timeout: float,
    *,
    detached_result_cleanup: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """以 task race 執行 bounded operation，timeout 或 caller cancellation 都立即脫離。"""
    operation_task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({operation_task}, timeout=max(0.0, timeout))
    except asyncio.CancelledError:
        _cancel_task_without_waiting(
            operation_task,
            detached_result_cleanup=detached_result_cleanup,
        )
        raise
    if operation_task in done:
        return await operation_task
    _cancel_task_without_waiting(
        operation_task,
        detached_result_cleanup=detached_result_cleanup,
    )
    raise _BoundedOperationTimeout


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
        headers: Mapping[str, str],
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
        self.headers = _normalize_response_headers(headers)
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
        """逐段產生 response body，並執行設定與呼叫端指定的大小限制。"""
        limit = self._max_bytes if max_bytes is None else min(self._max_bytes, max_bytes)
        is_chunked = _has_chunked_transfer_encoding(self.headers)
        content_length = self.content_length
        if content_length is not None and content_length > limit:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")

        if is_chunked:
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
        """以 best-effort 方式關閉 upstream connection，不讓 cleanup raw 例外外洩。"""
        try:
            self._writer.close()
        except (OSError, RuntimeError):
            return
        try:
            timeout = self._remaining_timeout()
            await _await_bounded(self._writer.wait_closed(), timeout)
        except (_BoundedOperationTimeout, AppError, TimeoutError, OSError):
            return
        except RuntimeError:
            return

    @property
    def content_length(self) -> int | None:
        """回傳通過格式與大小上限驗證的 Content-Length，缺少時回傳 None。"""
        value = self.headers.get("content-length")
        if value is None:
            return None
        if not value or any(char < "0" or char > "9" for char in value):
            raise AppError("upstream_media_invalid", "The media response length is invalid.")
        try:
            length = int(value)
        except ValueError as error:
            raise AppError(
                "upstream_media_invalid", "The media response length is invalid."
            ) from error
        if length > self._max_bytes:
            raise AppError("upstream_media_invalid", "The media response exceeds its size limit.")
        return length

    async def _iter_chunked(self, *, limit: int, chunk_size: int) -> AsyncIterator[bytes]:
        """嚴格解析 chunk framing、完整 trailer，並執行 body 大小上限。"""
        bytes_read = 0
        while True:
            line = await self._read_until(b"\r\n")
            if not line.endswith(b"\r\n"):
                raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
            size_token, separator, extensions = line[:-2].partition(b";")
            if not _is_ascii_hex(size_token):
                raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
            if separator:
                _validate_chunk_extensions(separator + extensions)
            size = int(size_token, 16)
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = await self._read_until(b"\r\n")
                    trailer_bytes += len(trailer)
                    if trailer_bytes > _MAX_CHUNK_TRAILER_BYTES:
                        raise AppError(
                            "upstream_media_invalid", "The media response trailers are invalid."
                        )
                    if not trailer.endswith(b"\r\n"):
                        raise AppError(
                            "upstream_media_invalid", "The media response chunks are invalid."
                        )
                    if trailer == b"\r\n":
                        return
                    _validate_trailer_line(trailer)
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
        timeout = self._remaining_timeout()
        try:
            return await _await_bounded(self._reader.read(size), timeout)
        except _BoundedOperationTimeout as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error
        except OSError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error

    async def _read_until(self, separator: bytes) -> bytes:
        """Read a protocol line before the configured upstream deadline."""
        timeout = self._remaining_timeout()
        try:
            return await _await_bounded(self._reader.readuntil(separator), timeout)
        except asyncio.IncompleteReadError as error:
            raise AppError("upstream_media_invalid", "The media response was truncated.") from error
        except _BoundedOperationTimeout as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error
        except (asyncio.LimitOverrunError, OSError) as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error

    async def _read_exactly(self, size: int) -> bytes:
        """Read an exact protocol section before the configured upstream deadline."""
        timeout = self._remaining_timeout()
        try:
            return await _await_bounded(self._reader.readexactly(size), timeout)
        except asyncio.IncompleteReadError as error:
            raise AppError("upstream_media_invalid", "The media response was truncated.") from error
        except _BoundedOperationTimeout as error:
            raise AppError("upstream_media_invalid", "The media response timed out.") from error
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The media response could not be read."
            ) from error
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
        total_timeout: float | None = None,
    ) -> None:
        """保存 outbound request 的目的地與資源限制。"""
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
        """取得 URL，並對每次允許的 redirect 重新執行驗證。"""
        current_url = url
        request_headers = normalize_request_headers(headers or {})
        deadline = None if self.total_timeout is None else time.monotonic() + self.total_timeout
        for redirect_count in range(self.max_redirects + 1):
            target = await self._validate_target(current_url, deadline=deadline)
            try:
                response = await self._fetch_target(target, request_headers, deadline=deadline)
            except AppError:
                raise
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                raise AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                ) from error
            except OSError as error:
                raise AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                ) from error
            except RuntimeError as error:
                raise AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                ) from error
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            try:
                await response.close()
            except Exception:
                pass
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
        deadline: float | None,
    ) -> ValidatedMediaTarget:
        """在不阻塞 event loop 的前提下驗證並解析單一媒體目的地。"""
        timeout = self._remaining_timeout(deadline, self.read_timeout)
        try:
            return await _await_bounded(asyncio.to_thread(self.policy.validate_url, url), timeout)
        except _BoundedOperationTimeout as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request failed."
            ) from error
        except OSError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request failed."
            ) from error
        except RuntimeError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request failed."
            ) from error

    async def _fetch_target(
        self,
        target: ValidatedMediaTarget,
        headers: Mapping[str, str],
        *,
        deadline: float | None,
    ) -> MediaResponse:
        """建立一條 pinned TLS connection，並解析其 response headers。"""
        context = ssl.create_default_context()
        try:
            connect_timeout = self._remaining_timeout(deadline, self.connect_timeout)
            reader, writer = await _await_bounded(
                asyncio.open_connection(
                    str(target.address),
                    target.port,
                    ssl=context,
                    server_hostname=target.hostname,
                ),
                connect_timeout,
                detached_result_cleanup=_close_detached_open_connection,
            )
        except _BoundedOperationTimeout as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        except TimeoutError as error:
            raise AppError(
                "upstream_media_invalid", "The upstream media request failed."
            ) from error
        path = target.path
        if target.query:
            path = f"{path}?{target.query}"
        request_lines = [f"GET {path} HTTP/1.1", f"Host: {target.hostname}", "Connection: close"]
        request_lines.extend(f"{name}: {value}" for name, value in headers.items())
        try:
            writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
            write_timeout = self._remaining_timeout(deadline, self.connect_timeout)
            await _await_bounded(writer.drain(), write_timeout)
        except _BoundedOperationTimeout as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        except (TimeoutError, OSError, RuntimeError) as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media request failed."
            ) from error
        except BaseException:
            await self._close_writer(writer)
            raise

        try:
            header_timeout = self._remaining_timeout(deadline, self.read_timeout)
            raw_headers = await _await_bounded(reader.readuntil(b"\r\n\r\n"), header_timeout)
            status_code, response_headers = _parse_response_headers(raw_headers)
        except _BoundedOperationTimeout as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media request timed out."
            ) from error
        except TimeoutError as error:
            await self._close_writer(writer)
            raise AppError(
                "upstream_media_invalid", "The upstream media headers were invalid."
            ) from error
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            OSError,
            RuntimeError,
        ) as error:
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
        try:
            writer.close()
        except asyncio.CancelledError:
            raise
        except BaseException:
            return
        timeout = max(0.0, min(self.read_timeout, 1.0))
        try:
            await _await_bounded(writer.wait_closed(), timeout)
        except asyncio.CancelledError:
            raise
        except BaseException:
            return

    def _remaining_timeout(self, deadline: float | None, maximum: float) -> float:
        """回傳單次 operation timeout，或有 deadline 時取其剩餘時間較小者。"""
        if deadline is None:
            return maximum
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("upstream_media_invalid", "The upstream media request timed out.")
        return min(maximum, remaining)


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    """消耗脫離主流程的 task 例外，避免未觀察例外警告。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _observe_task_result(
    task: asyncio.Future[Any],
    detached_result_cleanup: Callable[[Any], Awaitable[None]] | None,
) -> None:
    """觀察 detached task 的結果，必要時另行啟動 bounded result cleanup。"""
    if task.cancelled():
        return
    try:
        result = task.result()
    except BaseException:
        return
    if detached_result_cleanup is None:
        return
    try:
        cleanup_task = asyncio.ensure_future(detached_result_cleanup(result))
    except BaseException:
        return
    cleanup_task.add_done_callback(_consume_task_exception)


def _cancel_task_without_waiting(
    task: asyncio.Future[Any],
    *,
    detached_result_cleanup: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """取消 task 並觀察例外，必要時清理 cancellation-resistant 的成功結果。"""
    if task.done():
        _observe_task_result(task, detached_result_cleanup)
        return
    task.cancel()
    task.add_done_callback(
        lambda completed_task: _observe_task_result(
            completed_task,
            detached_result_cleanup,
        )
    )


async def _close_detached_open_connection(
    result: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    """關閉 detached open connection 的 writer，並 bounded wait_closed。"""
    _reader, writer = result
    try:
        writer.close()
        await _await_bounded(
            writer.wait_closed(),
            _DETACHED_CONNECTION_CLEANUP_TIMEOUT_SECONDS,
        )
    except BaseException:
        return


def _parse_response_headers(raw_headers: bytes) -> tuple[int, dict[str, str]]:
    """解析受大小限制的 HTTP status line，並將 response header 名稱轉為小寫。"""
    try:
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        version, status_text, _reason_phrase = lines[0].split(" ", 2)
        if (
            version != "HTTP/1.1"
            or len(status_text) != 3
            or not all(char.isascii() and "0" <= char <= "9" for char in status_text)
        ):
            raise ValueError("unsupported HTTP version")
        status_code = int(status_text)
        if not 100 <= status_code <= 599:
            raise ValueError("unsupported status code")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, value = line.split(":", 1)
            _add_normalized_header(headers, name, value)
        _validate_framing_headers(headers)
        return status_code, headers
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise AppError(
            "upstream_media_invalid", "The media response headers are invalid."
        ) from error


def _normalize_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """將 response headers 正規化並集中執行欄位與 framing 驗證。"""
    try:
        normalized: dict[str, str] = {}
        for name, value in headers.items():
            _add_normalized_header(normalized, name, value)
        _validate_framing_headers(normalized)
        return normalized
    except (TypeError, ValueError) as error:
        raise AppError(
            "upstream_media_invalid", "The media response headers are invalid."
        ) from error


def _add_normalized_header(headers: dict[str, str], name: str, value: str) -> None:
    """驗證單一 header 並加入 canonical lowercase mapping。"""
    if not isinstance(name, str) or not isinstance(value, str):
        raise ValueError("header name and value must be strings")
    if not _is_header_token(name):
        raise ValueError("invalid header field name")
    if not _is_safe_header_value(value):
        raise ValueError("invalid header field value")
    header_name = name.lower()
    if (
        header_name in {"content-length", "transfer-encoding", "content-encoding"}
        and header_name in headers
    ):
        raise ValueError("duplicate framing header")
    normalized_value = value.strip(" \t")
    if header_name == "content-length" and not _is_valid_content_length(normalized_value):
        raise ValueError("invalid content length")
    if header_name == "content-encoding":
        if normalized_value.lower() != "identity":
            raise ValueError("unsupported content encoding")
        normalized_value = "identity"
    headers[header_name] = normalized_value


def _validate_framing_headers(headers: Mapping[str, str]) -> None:
    """集中驗證 Content-Length 與 Transfer-Encoding framing 規則。"""
    if "content-length" in headers and "transfer-encoding" in headers:
        raise ValueError("conflicting framing headers")
    value = headers.get("transfer-encoding")
    if value is None:
        return
    tokens = value.split(",")
    if len(tokens) != 1 or tokens[0].strip(" \t").lower() != "chunked":
        raise ValueError("unsupported transfer encoding")


def _validate_trailer_line(line: bytes) -> None:
    """驗證 chunk trailer 是合法的 ASCII token 欄位與安全 value。"""
    try:
        text = line[:-2].decode("iso-8859-1")
        name, value = text.split(":", 1)
    except (UnicodeDecodeError, ValueError) as error:
        raise AppError(
            "upstream_media_invalid", "The media response trailers are invalid."
        ) from error
    if (
        not _is_header_token(name)
        or name.lower() in _FORBIDDEN_TRAILER_FIELDS
        or not _is_safe_header_value(value)
    ):
        raise AppError("upstream_media_invalid", "The media response trailers are invalid.")


def _is_header_token(value: str) -> bool:
    """驗證 header field-name 是否完全由 ASCII token 字元構成。"""
    return bool(value) and all(
        char.isascii() and (char.isalnum() or char in _HEADER_TOKEN_EXTRA) for char in value
    )


def _is_safe_header_value(value: str) -> bool:
    """確認 header value 不含 CTL/DEL，但保留 HTAB 與 ISO-8859-1 obs-text。"""
    return all(char == "\t" or (ord(char) >= 0x20 and ord(char) != 0x7F) for char in value)


def _is_valid_content_length(value: str) -> bool:
    """確認 Content-Length 是非空且只含 ASCII decimal digits 的值。"""
    return bool(value) and all(char.isascii() and "0" <= char <= "9" for char in value)


def _is_ascii_hex(value: bytes) -> bool:
    """確認 chunk size token 非空且每個 byte 都是 ASCII hex digit。"""
    return bool(value) and all(
        0x30 <= byte <= 0x39 or 0x41 <= byte <= 0x46 or 0x61 <= byte <= 0x66 for byte in value
    )


def _is_ascii_token_byte(value: int) -> bool:
    """確認單一 byte 是 HTTP token 允許的 ASCII 字元。"""
    return (
        0x30 <= value <= 0x39
        or 0x41 <= value <= 0x5A
        or 0x61 <= value <= 0x7A
        or value in b"!#$%&'*+-.^_`|~"
    )


def _validate_chunk_extensions(extensions: bytes) -> None:
    """嚴格驗證 chunk extension，只接受 name 或 name=token 形式。"""
    index = 0
    while index < len(extensions):
        if extensions[index] != ord(";"):
            raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
        index += 1
        name_start = index
        while index < len(extensions) and _is_ascii_token_byte(extensions[index]):
            index += 1
        if index == name_start:
            raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
        if index < len(extensions) and extensions[index] == ord("="):
            index += 1
            value_start = index
            while index < len(extensions) and _is_ascii_token_byte(extensions[index]):
                index += 1
            if index == value_start:
                raise AppError("upstream_media_invalid", "The media response chunks are invalid.")
        if index < len(extensions) and extensions[index] != ord(";"):
            raise AppError("upstream_media_invalid", "The media response chunks are invalid.")


def _has_chunked_transfer_encoding(headers: Mapping[str, str]) -> bool:
    """只接受沒有 CL 且單一 ASCII OWS 包圍的 chunked coding。"""
    normalized = _normalize_response_headers(headers)
    return "transfer-encoding" in normalized


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
    """只轉送驗證過的媒體標頭與安全的顯示方式控制標頭。"""
    is_chunked = _has_chunked_transfer_encoding(response.headers)
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    headers = build_preview_headers(filename) if preview else build_download_headers(filename)
    headers["Content-Type"] = content_type
    if not preview:
        if not is_chunked:
            content_length = response.content_length
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
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
