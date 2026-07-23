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


def test_thumbnail_settings_have_bounded_defaults() -> None:
    """Verify generated-preview resource settings use the approved defaults."""
    settings = Settings()

    assert settings.thumbnail_input_bytes == 32_000_000
    assert settings.thumbnail_output_bytes == 1_000_000
    assert settings.thumbnail_timeout_seconds == 10.0
    assert settings.thumbnail_concurrency == 1
    assert settings.thumbnail_cache_bytes == 32_000_000
    assert settings.thumbnail_max_edge == 640


def test_thumbnail_settings_reject_values_above_safe_bounds() -> None:
    """Verify generated-preview settings reject oversized or overly permissive values."""
    invalid = (
        {"thumbnail_input_bytes": 32_000_001},
        {"thumbnail_output_bytes": 1_000_001},
        {"thumbnail_timeout_seconds": 10.1},
        {"thumbnail_concurrency": 2},
        {"thumbnail_cache_bytes": 32_000_001},
        {"thumbnail_max_edge": 641},
    )

    for values in invalid:
        try:
            Settings(**values)
        except ValueError:
            continue
        raise AssertionError(f"Settings accepted invalid thumbnail values: {values}")


def test_cookie_file_settings_are_optional_and_platform_specific(tmp_path) -> None:
    """Verify each platform can independently use an optional cookie file."""
    instagram = tmp_path / "instagram.cookies.txt"
    instagram.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    settings = Settings(instagram_cookie_file=str(instagram))

    assert settings.instagram_cookie_file == str(instagram)
    assert settings.x_cookie_file is None


def test_cookie_file_settings_require_absolute_readable_non_empty_files(tmp_path) -> None:
    """Verify configured cookie files are checked without reading their contents into errors."""
    empty = tmp_path / "empty.cookies.txt"
    empty.touch()
    secret = "session-secret-value"
    relative = "relative.cookies.txt"

    for value in (str(empty), relative, str(tmp_path)):
        try:
            Settings(instagram_cookie_file=value)
        except ValueError as error:
            assert secret not in str(error)
        else:
            raise AssertionError(f"Settings accepted invalid cookie file: {value}")
