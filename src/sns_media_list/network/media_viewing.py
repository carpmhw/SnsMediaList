"""原始媒體檢視專用的有型別 Range 與被動內容政策。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from ..errors import AppError
from ..extractor.normalizer import sanitize_filename
from .media_client import MediaResponse, _parse_content_range, validate_preview_signature

VIEW_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
VIEW_VIDEO_TYPES = frozenset({"video/mp4", "video/webm"})


def validate_view_signature(content_type: str, media_class: str, prefix: bytes) -> bool:
    """在最多 512 bytes 的檔頭內驗證被動容器與 token 類型。"""
    prefix = prefix[:512]
    if media_class == "image" and content_type in VIEW_IMAGE_TYPES:
        return validate_preview_signature(content_type, prefix)
    if media_class != "video" or content_type not in VIEW_VIDEO_TYPES:
        return False
    if content_type == "video/mp4":
        return (
            len(prefix) >= 12 and int.from_bytes(prefix[:4], "big") >= 12 and prefix[4:8] == b"ftyp"
        )
    return prefix.startswith(b"\x1a\x45\xdf\xa3")


def build_view_headers(filename: str, content_type: str, metadata: ViewResponse) -> dict[str, str]:
    """僅由安全檔名與已驗證 metadata 建立被動 inline headers。"""
    safe_name = quote(sanitize_filename(filename), safe="")
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'none'; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'; sandbox"
        ),
    }
    if metadata.length is not None:
        headers["Content-Length"] = str(metadata.length)
    if metadata.content_range is not None:
        headers["Content-Range"] = metadata.content_range
        headers["Accept-Ranges"] = "bytes"
    return headers


class ViewRangeError(AppError):
    """攜帶可安全投影到 Content-Range 的可信來源長度。"""

    def __init__(self, total: int | None = None) -> None:
        """建立固定 416 錯誤，不保留任何來源錯誤文字。"""
        super().__init__("view_range_not_satisfiable", "無法提供此媒體範圍。")
        self.total = total


@dataclass(frozen=True, slots=True)
class ViewResponse:
    """保存已驗證的完整或單一片段 response metadata。"""

    length: int | None
    total: int | None
    content_range: str | None = None


def validate_view_response(
    response: MediaResponse, requested: ViewRange | None, *, max_bytes: int
) -> ViewResponse:
    """將 upstream status 與可信範圍驗證為可輸出的 metadata。"""
    length = response.content_length
    if requested is None:
        if (
            response.status_code != 200
            or "content-range" in response.headers
            or (length is not None and length > max_bytes)
        ):
            raise AppError("upstream_media_invalid", "媒體完整回應無效。")
        return ViewResponse(length, length)
    if response.status_code == 416:
        value = response.headers.get("content-range", "")
        match = re.fullmatch(r"bytes \*/(0|[1-9][0-9]{0,19})", value, flags=re.ASCII)
        total = int(match[1]) if match else None
        raise ViewRangeError(total if total is not None and total <= max_bytes else None)
    if response.status_code != 206:
        raise AppError("upstream_media_invalid", "媒體片段回應無效。")
    value = response.headers.get("content-range", "")
    if len(value) > 128:
        raise AppError("upstream_media_invalid", "媒體範圍回應無效。")
    parsed = _parse_content_range(value)
    if parsed.total > max_bytes:
        raise AppError("upstream_media_invalid", "媒體來源超過大小上限。")
    if requested.start is None:
        assert requested.end is not None
        start, end = max(0, parsed.total - requested.end), parsed.total - 1
    else:
        start = requested.start
        if start >= parsed.total:
            raise ViewRangeError(parsed.total)
        end = (
            min(requested.end, parsed.total - 1) if requested.end is not None else parsed.total - 1
        )
    expected_length = end - start + 1
    if (
        parsed.start != start
        or parsed.end != end
        or (length is not None and length != expected_length)
    ):
        raise AppError("upstream_media_invalid", "媒體片段不符合請求。")
    return ViewResponse(expected_length, parsed.total, f"bytes {start}-{end}/{parsed.total}")


@dataclass(frozen=True, slots=True)
class ViewRange:
    """保存已通過入口語法驗證的單一 byte range。"""

    start: int | None
    end: int | None

    @property
    def header(self) -> str:
        """由已解析數值產生 outbound canonical Range。"""
        if self.start is None:
            return f"bytes=-{self.end}"
        return f"bytes={self.start}-{self.end if self.end is not None else ''}"


def parse_view_range(values: Sequence[str]) -> ViewRange | None:
    """只接受有界、單一 ASCII bytes range，拒絕重複與寬鬆語法。"""
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > 128:
        raise AppError("view_range_not_satisfiable", "無法提供此媒體範圍。")
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", values[0], flags=re.ASCII)
    if match is None:
        raise AppError("view_range_not_satisfiable", "無法提供此媒體範圍。")
    first, last = match.groups()
    start = int(first) if first else None
    end = int(last) if last else None
    if (start is None and (end is None or end == 0)) or (
        start is not None and end is not None and end < start
    ):
        raise AppError("view_range_not_satisfiable", "無法提供此媒體範圍。")
    return ViewRange(start, end)
