"""Tests for application settings."""

from sns_media_list.config import Settings


def test_settings_have_bounded_defaults() -> None:
    """Verify safe default resource limits are present."""
    settings = Settings()

    assert settings.media_limit == 20
    assert settings.token_ttl_seconds == 600
    assert settings.extraction_timeout_seconds > 0
    assert settings.max_download_bytes > 0


def test_settings_reject_non_positive_limits() -> None:
    """Verify resource limits cannot be configured as non-positive values."""
    try:
        Settings(media_limit=0)
    except ValueError as error:
        assert "media_limit" in str(error)
    else:
        raise AssertionError("Settings accepted an invalid media limit")
