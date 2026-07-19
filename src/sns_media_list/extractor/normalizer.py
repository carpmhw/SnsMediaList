"""Normalize gallery-dl metadata into application-owned media records."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..errors import AppError


@dataclass(frozen=True, slots=True)
class GalleryItem:
    """Represent one sanitized media record emitted by gallery-dl."""

    platform: str
    post_url: str
    post_id: str
    index: int
    media_type: str
    source_url: str | None
    author: str | None = None
    description: str | None = None
    preview_url: str | None = None
    extension: str = "bin"
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    progressive: bool = True
    bitrate: int = 0
    extractor_type: int = 0


@dataclass(frozen=True, slots=True)
class NormalizedMedia:
    """Represent one selected direct media item before token issuance."""

    platform: str
    post_id: str
    index: int
    media_type: str
    source_url: str
    preview_source_url: str | None
    filename: str
    width: int | None
    height: int | None
    duration: float | None
    source_kind: str


@dataclass(frozen=True, slots=True)
class NormalizedExtraction:
    """Represent a complete ordered extraction without public tokens."""

    platform: str
    post_url: str
    post_id: str
    author: str | None
    description: str | None
    unavailable_media_count: int
    items: tuple[NormalizedMedia, ...]


def normalize_gallery_output(
    lines: Iterable[str | Mapping[str, object]],
    *,
    media_limit: int = 20,
) -> NormalizedExtraction:
    """Parse and normalize sanitized gallery-dl JSON records."""
    records = [_parse_record(line) for line in lines]
    if not records:
        raise AppError("extraction_failed", "The extractor returned no metadata.")

    unavailable = 0
    candidates: list[GalleryItem] = []
    for item in records:
        if not item.source_url or not item.progressive:
            unavailable += 1
            continue
        _validate_source_url(item.source_url)
        candidates.append(item)

    selected = select_best_variants(candidates)
    if len(selected) > media_limit:
        raise AppError("extraction_limit_exceeded", "The post contains too many media items.")

    first = records[0]
    items = tuple(_to_normalized_media(item) for item in selected)
    return NormalizedExtraction(
        platform=first.platform,
        post_url=first.post_url,
        post_id=first.post_id,
        author=_optional_text(records, "author"),
        description=_optional_text(records, "description"),
        unavailable_media_count=unavailable,
        items=items,
    )


def ensure_downloadable_media(result: NormalizedExtraction) -> NormalizedExtraction:
    """Raise the stable no-media error when normalization selected nothing."""
    if not result.items:
        raise AppError("no_media", "No directly downloadable media was found.")
    return result


def select_best_variants(items: Iterable[GalleryItem]) -> list[GalleryItem]:
    """Select one direct variant per source media index using platform rules."""
    grouped: dict[tuple[str, str, int], list[GalleryItem]] = defaultdict(list)
    for item in items:
        grouped[(item.platform, item.post_id, item.index)].append(item)

    selected: list[GalleryItem] = []
    for variants in grouped.values():
        platform = variants[0].platform
        if platform == "instagram":
            choice = max(
                variants,
                key=lambda item: (
                    item.width or 0,
                    item.height or 0,
                    item.extractor_type,
                ),
            )
        else:
            choice = max(
                variants,
                key=lambda item: (item.bitrate, item.width or 0, item.height or 0),
            )
        selected.append(choice)
    return sorted(selected, key=lambda item: item.index)


def _parse_record(line: str | Mapping[str, object]) -> GalleryItem:
    """Parse one JSON record and map invalid data to a safe application error."""
    try:
        record = json.loads(line) if isinstance(line, str) else dict(line)
        raw_type = str(record["type"])
        media_type = "video" if raw_type in {"video", "animated_gif"} else "image"
        post_url = str(record["post_url"])
        platform = str(record["platform"])
        post_id = str(record["post_id"])
        author = _optional_text_value(record.get("author"))
        description = _optional_text_value(record.get("description"))
        index = _optional_int(record["num"])
        extension = _safe_extension(str(record.get("extension") or "bin"))
        source_url = _optional_url(record.get("url"))
        preview_url = _optional_url(record.get("preview_url"))
        if platform not in {"instagram", "x"} or index is None or index < 1:
            raise ValueError("invalid platform or index")
        return GalleryItem(
            platform=platform,
            post_url=post_url,
            post_id=post_id,
            author=author,
            description=description,
            index=index,
            media_type=media_type,
            source_url=source_url,
            preview_url=preview_url,
            extension=extension,
            width=_optional_int(record.get("width")),
            height=_optional_int(record.get("height")),
            duration=_optional_float(record.get("duration")),
            progressive=bool(record.get("progressive", True)),
            bitrate=_optional_int(record.get("bitrate")) or 0,
            extractor_type=_optional_int(record.get("extractor_type")) or 0,
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise AppError("extraction_failed", "The extractor returned invalid metadata.") from error


def _to_normalized_media(item: GalleryItem) -> NormalizedMedia:
    """Convert one selected gallery item to an internal media record."""
    extension = item.extension or ("mp4" if item.media_type == "video" else "jpg")
    filename = sanitize_filename(f"{item.platform}-{item.post_id}-{item.index}.{extension}")
    return NormalizedMedia(
        platform=item.platform,
        post_id=item.post_id,
        index=item.index,
        media_type=item.media_type,
        source_url=item.source_url or "",
        preview_source_url=item.preview_url,
        filename=filename,
        width=item.width,
        height=item.height,
        duration=item.duration,
        source_kind="progressive",
    )


def sanitize_filename(value: str) -> str:
    """Remove path, control, and response-header characters from a filename."""
    cleaned = re.sub(r"[\x00-\x1f\x7f\\/:\"<>|?*\r\n]+", "-", value)
    return cleaned.strip(" .") or "media.bin"


def build_media_request_headers(platform: str) -> dict[str, str]:
    """Build the fixed request headers allowed for a platform CDN."""
    if platform == "instagram":
        referer = "https://www.instagram.com/"
    elif platform == "x":
        referer = "https://x.com/"
    else:
        raise AppError("extraction_failed", "The extractor returned an unknown platform.")
    return normalize_request_headers(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
            ),
            "Referer": referer,
        }
    )


def normalize_request_headers(headers: Mapping[str, object]) -> dict[str, str]:
    """Allow only safe header names and values for upstream media requests."""
    allowed = {"User-Agent", "Referer"}
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if name not in allowed or not isinstance(value, str) or "\r" in value or "\n" in value:
            raise AppError("extraction_failed", "The extractor returned unsafe request headers.")
        normalized[name] = value
    return normalized


def _safe_extension(value: str) -> str:
    """Keep only a conservative lowercase file extension."""
    extension = re.sub(r"[^a-zA-Z0-9]", "", value).lower()
    return extension[:10] or "bin"


def _validate_source_url(value: str) -> None:
    """Reject non-HTTPS or credential-bearing upstream media URLs."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise AppError("extraction_failed", "The extractor returned an unsafe media URL.")


def _optional_url(value: object) -> str | None:
    """Convert a nullable URL field to text without accepting empty values."""
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    """Convert a nullable integer field while preserving missing values."""
    return None if value is None else int(str(value))


def _optional_float(value: object) -> float | None:
    """Convert a nullable numeric field while preserving missing values."""
    return None if value is None else float(str(value))


def _optional_text(records: list[GalleryItem], field_name: str) -> str | None:
    """Return optional metadata from a record when the field is available."""
    value: object = getattr(records[0], field_name)
    return _optional_text_value(value)


def _optional_text_value(value: object) -> str | None:
    """Convert nullable metadata to text without inventing a value."""
    if value is None or value == "":
        return None
    return str(value)
