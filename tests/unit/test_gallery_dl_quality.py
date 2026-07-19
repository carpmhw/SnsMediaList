"""Tests for platform-specific direct media quality selection."""

from sns_media_list.extractor.normalizer import GalleryItem, select_best_variants


def test_instagram_selects_largest_direct_video_dimensions() -> None:
    """Verify Instagram direct variants prefer width, height, then type."""
    variants = [
        GalleryItem(
            platform="instagram",
            post_url="https://www.instagram.com/reel/1/",
            post_id="1",
            index=1,
            media_type="video",
            source_url="https://cdn.example/720.mp4",
            extension="mp4",
            width=720,
            height=1280,
            progressive=True,
            extractor_type=101,
        ),
        GalleryItem(
            platform="instagram",
            post_url="https://www.instagram.com/reel/1/",
            post_id="1",
            index=1,
            media_type="video",
            source_url="https://cdn.example/1080.mp4",
            extension="mp4",
            width=1080,
            height=1920,
            progressive=True,
            extractor_type=101,
        ),
    ]

    selected = select_best_variants(variants)

    assert selected[0].source_url.endswith("1080.mp4")


def test_x_selects_highest_declared_bitrate() -> None:
    """Verify X direct variants prefer declared bitrate."""
    variants = [
        GalleryItem(
            platform="x",
            post_url="https://x.com/creator/status/1",
            post_id="1",
            index=1,
            media_type="video",
            source_url="https://cdn.example/low.mp4",
            extension="mp4",
            width=1280,
            height=720,
            bitrate=512000,
            progressive=True,
        ),
        GalleryItem(
            platform="x",
            post_url="https://x.com/creator/status/1",
            post_id="1",
            index=1,
            media_type="video",
            source_url="https://cdn.example/high.mp4",
            extension="mp4",
            width=1280,
            height=720,
            bitrate=2176000,
            progressive=True,
        ),
    ]

    selected = select_best_variants(variants)

    assert selected[0].source_url.endswith("high.mp4")
