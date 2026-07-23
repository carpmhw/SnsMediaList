"""Tests for public and private media models."""

from sns_media_list.models import MediaItem, PrivateMediaRecord


def test_public_media_model_excludes_private_fields() -> None:
    """Verify upstream details cannot leak through the public model."""
    media = MediaItem(
        token="opaque",
        media_type="image",
        filename="x.jpg",
        download_url="/api/media/opaque/download",
    )

    assert "source_url" not in media.model_dump()
    assert "request_headers" not in media.model_dump()


def test_private_record_contains_upstream_details() -> None:
    """Verify private storage can retain data needed for streaming."""
    record = PrivateMediaRecord(
        token="opaque",
        purpose="download",
        source_url="https://pbs.twimg.com/media/x.jpg",
        media_class="image",
        filename="x.jpg",
        platform="x",
        expires_at=100.0,
        request_headers={"User-Agent": "test"},
    )

    assert record.source_url.startswith("https://")


def test_private_preview_record_keeps_generation_mode_private() -> None:
    """Verify preview generation mode is retained only in the private record."""
    record = PrivateMediaRecord(
        token="opaque",
        purpose="preview",
        source_url="https://video.twimg.com/video.mp4",
        media_class="video",
        filename="x.mp4",
        platform="x",
        expires_at=100.0,
        request_headers={"User-Agent": "test"},
        preview_mode="generated",
    )

    assert record.preview_mode == "generated"
    assert (
        "preview_mode"
        not in MediaItem(
            token="opaque",
            media_type="video",
            filename="x.mp4",
            download_url="/api/media/opaque/download",
        ).model_dump()
    )
