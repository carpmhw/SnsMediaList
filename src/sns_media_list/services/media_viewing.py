"""協調原始媒體檢視與有界檔頭探測，不處理 attachment 續傳。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from ..errors import AppError
from ..models import PrivateMediaRecord
from ..network.media_client import MediaResponse
from ..network.media_viewing import (
    VIEW_IMAGE_TYPES,
    VIEW_VIDEO_TYPES,
    ViewRange,
    ViewResponse,
    validate_view_response,
    validate_view_signature,
)


@dataclass(slots=True)
class PreparedView:
    """保存開始 HTTP response 前已驗證的顯示資料與 body iterator。"""

    metadata: ViewResponse
    content_type: str
    body: AsyncIterator[bytes]


async def prepare_view(
    record: PrivateMediaRecord,
    requested: ViewRange | None,
    *,
    fetch: Callable[[ViewRange | None], Awaitable[MediaResponse]],
    close: Callable[[MediaResponse], Awaitable[None]],
    max_bytes: int,
) -> PreparedView:
    """以同一受限 fetch／close 邊界完成檔頭探測與精確片段預檢。"""
    probe_bytes = 0
    probe_type: str | None = None
    probe_total: int | None = None

    def media_type(response: MediaResponse) -> str:
        """要求來源 MIME 與 token 類別及被動格式 allowlist 相符。"""
        value = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        allowed = VIEW_IMAGE_TYPES if record.media_class == "image" else VIEW_VIDEO_TYPES
        if value not in allowed:
            raise AppError("upstream_media_invalid", "媒體格式不支援原始檢視。")
        return value

    if requested is not None and (
        requested.start != 0 or (requested.end is not None and requested.end < 11)
    ):
        probe = await fetch(None)
        probe_metadata = validate_view_response(probe, None, max_bytes=max_bytes)
        probe_type = media_type(probe)
        probe_total = probe_metadata.total
        prefix = bytearray()
        async for chunk in probe.iter_bytes(chunk_size=12, max_bytes=max_bytes):
            prefix.extend(chunk)
            probe_bytes += len(chunk)
            if len(prefix) >= 12:
                break
        if not validate_view_signature(probe_type, record.media_class, bytes(prefix)):
            raise AppError("upstream_media_invalid", "媒體檔頭不符合格式。")
        await close(probe)

    response = await fetch(requested)
    metadata = validate_view_response(response, requested, max_bytes=max_bytes)
    content_type = media_type(response)
    if probe_type is not None and (
        content_type != probe_type or (probe_total is not None and metadata.total != probe_total)
    ):
        raise AppError("upstream_media_invalid", "媒體探測與片段不一致。")
    iterator = response.iter_bytes(max_bytes=max_bytes - probe_bytes)
    first = bytearray()
    async for chunk in iterator:
        first.extend(chunk)
        if probe_type is not None or len(first) >= 12:
            break
    if not first or (
        probe_type is None
        and not validate_view_signature(content_type, record.media_class, bytes(first[:512]))
    ):
        raise AppError("upstream_media_invalid", "媒體檔頭不符合格式。")

    async def body() -> AsyncIterator[bytes]:
        """恰好輸出已讀取 bytes 並驗證片段長度與累計傳輸上限。"""
        delivered = len(first)
        if metadata.length is not None and delivered > metadata.length:
            raise AppError("upstream_media_invalid", "媒體片段超出宣告長度。")
        yield bytes(first)
        async for chunk in iterator:
            delivered += len(chunk)
            if delivered + probe_bytes > max_bytes or (
                metadata.length is not None and delivered > metadata.length
            ):
                raise AppError("upstream_media_invalid", "媒體超過傳輸上限。")
            yield chunk
        if metadata.length is not None and delivered != metadata.length:
            raise AppError("upstream_media_invalid", "媒體片段提前結束。")

    return PreparedView(metadata, content_type, body())
