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


def test_instagram_display_url_becomes_proxy_preview_source() -> None:
    """Verify Instagram poster metadata is used without creating a new media item."""
    result = normalize_gallery_output(fixture_lines("instagram-video-poster.jsonl"))

    assert len(result.items) == 1
    assert result.items[0].preview_source_url == (
        "https://scontent.cdninstagram.com/reel-1-poster.jpg"
    )


def test_instagram_resolved_video_url_is_not_mislabeled_as_image() -> None:
    """Verify current Instagram post records with video URLs retain the video class."""
    result = normalize_gallery_output(
        [
            {
                "platform": "instagram",
                "post_url": "https://www.instagram.com/reel/REEL002/",
                "post_id": "REEL002",
                "num": 1,
                "type": "post",
                "url": "https://scontent.cdninstagram.com/reel-2.mp4",
                "video_url": "https://scontent.cdninstagram.com/reel-2.mp4",
                "extension": "mp4",
                "progressive": True,
            }
        ]
    )

    assert result.items[0].media_type == "video"


def test_instagram_story_image_ignores_audio_metadata() -> None:
    """Verify Story soundtrack metadata does not create a separate audio item."""
    result = normalize_gallery_output(fixture_lines("instagram-story-image.jsonl"))

    assert [item.media_type for item in result.items] == ["image"]


def test_instagram_story_video_keeps_one_primary_video() -> None:
    """Verify a video Story remains one primary video item after normalization."""
    result = normalize_gallery_output(fixture_lines("instagram-story-video.jsonl"))

    assert [item.media_type for item in result.items] == ["video"]


def test_x_original_image_gets_supported_small_cdn_preview() -> None:
    """Verify the tested X image query transformation preserves the original download URL."""
    result = normalize_gallery_output(fixture_lines("x-image-no-preview.jsonl"))

    assert result.items[0].source_url.endswith("name=orig")
    assert result.items[0].preview_source_url.endswith("name=small")


@pytest.mark.parametrize(
    "source_url",
    [
        "https://pbs.twimg.com:8443/media/IMAGE001.jpg?format=jpg&name=orig",
        "https://pbs.twimg.com/media/IMAGE001.jpg?format=mp4&name=orig",
    ],
)
def test_x_image_rewrite_rejects_unsafe_preview_shape(source_url: str) -> None:
    """Verify untested X image URL shapes fall back instead of creating proxy previews."""
    result = normalize_gallery_output(
        [
            {
                "platform": "x",
                "post_url": "https://x.com/public_creator/status/1001",
                "post_id": "1001",
                "num": 1,
                "type": "image",
                "url": source_url,
                "extension": "jpg",
                "progressive": True,
            }
        ]
    )

    assert result.items[0].preview_source_url is None


@pytest.mark.parametrize(
    "source_url",
    [
        "https://pbs.twimg.com/media/IMAGE001.jpg\r\nX-Injected: value",
        "https://pbs.twimg.com/media/é.jpg",
    ],
)
def test_media_source_rejects_request_line_unsafe_characters(source_url: str) -> None:
    """Verify normalized media URLs contain only safe HTTP request-line characters."""
    with pytest.raises(AppError) as exc_info:
        normalize_gallery_output(
            [
                {
                    "platform": "x",
                    "post_url": "https://x.com/creator/status/1001",
                    "post_id": "1001",
                    "num": 1,
                    "type": "image",
                    "url": source_url,
                    "extension": "jpg",
                    "progressive": True,
                }
            ]
        )

    assert exc_info.value.code == "extraction_failed"


def test_unknown_preview_host_falls_back_to_generated_mode() -> None:
    """Verify untrusted preview metadata is discarded instead of being proxied."""
    result = normalize_gallery_output(
        [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "video",
                "url": "https://video.twimg.com/video.mp4",
                "preview_url": "https://example.invalid/poster.jpg",
                "extension": "mp4",
                "progressive": True,
            }
        ]
    )

    assert result.items[0].preview_source_url is None


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
