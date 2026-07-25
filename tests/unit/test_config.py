"""Tests for application settings."""

from sns_media_list.config import Settings


def test_settings_have_bounded_defaults() -> None:
    """Verify safe default resource limits are present."""
    settings = Settings()

    assert settings.media_limit == 20
    assert settings.token_ttl_seconds == 600
    assert settings.extraction_timeout_seconds > 0
    assert settings.max_download_bytes > 0
    assert settings.max_downloads_per_client == 2
    assert settings.extraction_body_limit_bytes == 4_096
    assert settings.rate_limit_window_seconds == 60.0
    assert settings.rate_limit_extraction_attempts == 10
    assert settings.rate_limit_media_attempts == 120
    assert settings.rate_limit_identity_capacity == 2_048
    assert settings.media_response_timeout_seconds == 120.0
    assert settings.generated_previews_enabled is False


def test_settings_reject_non_positive_limits() -> None:
    """Verify resource limits cannot be configured as non-positive values."""
    try:
        Settings(media_limit=0)
    except ValueError as error:
        assert "media_limit" in str(error)
    else:
        raise AssertionError("Settings accepted an invalid media limit")


def test_security_settings_accept_environment_overrides(monkeypatch) -> None:
    """Verify security settings are loaded through the SNS_MEDIA environment prefix."""
    monkeypatch.setenv("SNS_MEDIA_EXTRACTION_BODY_LIMIT_BYTES", "8192")
    monkeypatch.setenv("SNS_MEDIA_RATE_LIMIT_WINDOW_SECONDS", "30")
    monkeypatch.setenv("SNS_MEDIA_RATE_LIMIT_EXTRACTION_ATTEMPTS", "3")
    monkeypatch.setenv("SNS_MEDIA_RATE_LIMIT_MEDIA_ATTEMPTS", "12")
    monkeypatch.setenv("SNS_MEDIA_RATE_LIMIT_IDENTITY_CAPACITY", "64")
    monkeypatch.setenv("SNS_MEDIA_MAX_DOWNLOADS_PER_CLIENT", "3")
    monkeypatch.setenv("SNS_MEDIA_MEDIA_RESPONSE_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("SNS_MEDIA_GENERATED_PREVIEWS_ENABLED", "true")

    settings = Settings()

    assert settings.extraction_body_limit_bytes == 8_192
    assert settings.rate_limit_window_seconds == 30.0
    assert settings.rate_limit_extraction_attempts == 3
    assert settings.rate_limit_media_attempts == 12
    assert settings.rate_limit_identity_capacity == 64
    assert settings.max_downloads_per_client == 3
    assert settings.media_response_timeout_seconds == 90.0
    assert settings.generated_previews_enabled is True


def test_security_settings_reject_values_outside_safe_bounds() -> None:
    """Verify request and response security limits cannot be unbounded or disabled."""
    invalid = (
        {"extraction_body_limit_bytes": 0},
        {"extraction_body_limit_bytes": 65_537},
        {"rate_limit_window_seconds": 0},
        {"rate_limit_window_seconds": 3_601},
        {"rate_limit_extraction_attempts": 0},
        {"rate_limit_extraction_attempts": 11},
        {"rate_limit_media_attempts": 0},
        {"rate_limit_media_attempts": 121},
        {"rate_limit_identity_capacity": 0},
        {"rate_limit_identity_capacity": 2_049},
        {"max_downloads_per_client": 0},
        {"media_response_timeout_seconds": 0},
        {"media_response_timeout_seconds": 3_601},
    )

    for values in invalid:
        try:
            Settings(**values)
        except ValueError:
            continue
        raise AssertionError(f"Settings accepted invalid security values: {values}")


def test_trusted_proxy_cidrs_are_valid_and_not_default_open() -> None:
    """Verify proxy identity configuration rejects malformed or global networks."""
    assert Settings(trusted_proxy_cidrs=("10.0.0.0/8",)).trusted_proxy_cidrs == ("10.0.0.0/8",)

    for value in (("not-a-cidr",), ("0.0.0.0/0",), ("::/0",)):
        try:
            Settings(trusted_proxy_cidrs=value)
        except ValueError:
            continue
        raise AssertionError(f"Settings accepted an unsafe proxy CIDR: {value}")


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
