"""Contract tests for normalized gallery-dl output."""

import json
from pathlib import Path

import pytest

from sns_media_list.errors import AppError
from sns_media_list.extractor.normalizer import (
    build_media_request_headers,
    ensure_downloadable_media,
    normalize_gallery_output,
    normalize_request_headers,
    sanitize_filename,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gallery_dl"


def fixture_lines(name: str) -> list[str]:
    """Read sanitized JSONL fixture lines for one gallery-dl scenario."""
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_mixed_carousel_preserves_source_order() -> None:
    """Verify mixed Instagram media remains in the original order."""
    result = normalize_gallery_output(fixture_lines("instagram-carousel.jsonl"))

    assert [item.media_type for item in result.items] == ["image", "video", "image"]
    assert [item.index for item in result.items] == [1, 2, 3]
    assert result.unavailable_media_count == 0


def test_x_animated_gif_is_normalized_as_video() -> None:
    """Verify X animated GIF entries use the video media class."""
    result = normalize_gallery_output(fixture_lines("x-gif.jsonl"))

    assert result.items[0].media_type == "video"
    assert result.items[0].source_kind == "progressive"


def test_adaptive_only_item_is_counted_unavailable() -> None:
    """Verify media without a direct URL is omitted and counted."""
    result = normalize_gallery_output(fixture_lines("instagram-adaptive-only.jsonl"))

    assert result.items == ()
    assert result.unavailable_media_count == 1


def test_all_unavailable_media_returns_no_media() -> None:
    """Verify an extraction with no direct media has a stable error."""
    result = normalize_gallery_output(fixture_lines("instagram-adaptive-only.jsonl"))

    with pytest.raises(AppError) as exc_info:
        ensure_downloadable_media(result)

    assert exc_info.value.code == "no_media"


def test_more_than_twenty_downloadable_items_is_rejected() -> None:
    """Verify the normalized media count is bounded before token issuance."""
    records = [
        {
            "platform": "x",
            "post_url": "https://x.com/creator/status/2000",
            "post_id": "2000",
            "num": index,
            "type": "image",
            "url": f"https://pbs.twimg.com/media/{index}.jpg",
            "extension": "jpg",
            "width": 100,
            "height": 100,
            "progressive": True,
        }
        for index in range(1, 22)
    ]

    with pytest.raises(AppError) as exc_info:
        normalize_gallery_output([json.dumps(record) for record in records])

    assert exc_info.value.code == "extraction_limit_exceeded"


def test_malformed_gallery_output_is_rejected() -> None:
    """Verify malformed extractor output does not become a public result."""
    with pytest.raises(AppError) as exc_info:
        normalize_gallery_output(["not-json"])

    assert exc_info.value.code == "extraction_failed"


def test_media_headers_are_fixed_per_platform() -> None:
    """Verify stored media headers contain only fixed safe values."""
    assert build_media_request_headers("instagram") == {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": "https://www.instagram.com/",
    }
    assert build_media_request_headers("x")["Referer"] == "https://x.com/"


def test_disallowed_media_header_is_rejected() -> None:
    """Verify credentials, host overrides, and CR/LF values cannot be stored."""
    with pytest.raises(AppError) as exc_info:
        normalize_request_headers({"Authorization": "Bearer secret"})

    assert exc_info.value.code == "extraction_failed"


def test_filename_removes_path_and_header_characters() -> None:
    """Verify source metadata cannot escape a download filename."""
    assert (
        sanitize_filename("../x\r\nContent-Disposition: evil.jpg")
        == "-x-Content-Disposition- evil.jpg"
    )
