"""Tests for supported post URL validation."""

import pytest

from sns_media_list.errors import AppError
from sns_media_list.url_validation import (
    validate_platform_redirect_target,
    validate_post_url,
    validate_redirect_chain,
)


@pytest.mark.parametrize(
    ("url", "platform", "target_id"),
    [
        ("https://www.instagram.com/p/ABC123/", "instagram", "ABC123"),
        (
            "https://www.instagram.com/moe_five/p/Da4YHf5mBgu/?hl=zh-tw",
            "instagram",
            "Da4YHf5mBgu",
        ),
        ("https://www.instagram.com/reel/ABC123/", "instagram", "ABC123"),
        ("https://x.com/user/status/123456789", "x", "123456789"),
        ("https://twitter.com/user/status/123456789?ref=share", "x", "123456789"),
    ],
)
def test_supported_public_post_urls(url: str, platform: str, target_id: str) -> None:
    """Verify accepted URL forms return platform and post identity."""
    parsed = validate_post_url(url)

    assert parsed.platform == platform
    assert parsed.target_id == target_id
    assert "ref=share" not in parsed.canonical_url


@pytest.mark.parametrize(
    ("url", "canonical_url", "target_id"),
    [
        (
            "https://www.instagram.com/stories/example.user/1234567890/?igsh=tracking&locale=en",
            "https://www.instagram.com/stories/example.user/1234567890/",
            "1234567890",
        ),
        (
            "https://instagram.com/stories/_creator123/7/",
            "https://www.instagram.com/stories/_creator123/7/",
            "7",
        ),
        pytest.param(
            "https://www.instagram.com/stories/encoded_marker/23/?marker=%23%40%3A",
            "https://www.instagram.com/stories/encoded_marker/23/",
            "23",
            id="encoded-delimiters-in-query",
        ),
        pytest.param(
            "https://www.instagram.com/stories/a/1/",
            "https://www.instagram.com/stories/a/1/",
            "1",
            id="one-character-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/30/",
            "https://www.instagram.com/stories/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/30/",
            "30",
            id="thirty-character-username",
        ),
    ],
)
def test_exact_instagram_story_urls_are_canonical_targets(
    url: str,
    canonical_url: str,
    target_id: str,
) -> None:
    """Verify exact Story URLs become canonical single-Story targets."""
    parsed = validate_post_url(url)

    assert parsed.platform == "instagram"
    assert parsed.kind == "story"
    assert parsed.target_id == target_id
    assert parsed.canonical_url == canonical_url


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(
            "https://www.instagram.com/stories/example.user/",
            id="account-wide",
        ),
        pytest.param("https://www.instagram.com/stories/me/", id="own-account-wide"),
        pytest.param("https://www.instagram.com/stories/", id="stories-tray"),
        pytest.param(
            "https://www.instagram.com/stories/highlights/1234567890/",
            id="highlight",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/not-numeric/",
            id="non-numeric-media-id",
        ),
        pytest.param(
            "https://www.instagram.com/stories/bad-user/1234567890/",
            id="invalid-username-character",
        ),
        pytest.param(
            "https://www.instagram.com/stories/./1234567890/",
            id="dot-only-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/../1234567890/",
            id="double-dot-only-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/.example/1234567890/",
            id="leading-dot-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example./1234567890/",
            id="trailing-dot-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example..user/1234567890/",
            id="consecutive-dots-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/exämple/1234567890/",
            id="non-ascii-username",
        ),
        pytest.param(
            "https://www.instagram.com/stories/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/1234567890/",
            id="username-too-long",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/1234567890",
            id="missing-trailing-slash",
        ),
        pytest.param(
            "https://www.instagram.com/stories//1234567890/",
            id="empty-username-segment",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user//1234567890/",
            id="empty-segment-before-media-id",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/1234567890/extra/",
            id="extra-path-component",
        ),
        pytest.param(
            "http://www.instagram.com/stories/example.user/1234567890/",
            id="non-https",
        ),
        pytest.param(
            "https://user:secret@www.instagram.com/stories/example.user/1234567890/",
            id="credentials",
        ),
        pytest.param(
            "https://@www.instagram.com/stories/example/1/",
            id="empty-username-userinfo",
        ),
        pytest.param(
            "https://:@www.instagram.com/stories/example/1/",
            id="empty-username-and-password-userinfo",
        ),
        pytest.param(
            "https://www.instagram.com:8443/stories/example.user/1234567890/",
            id="unsupported-port",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/1234567890/#fragment",
            id="fragment",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/1234567890/#",
            id="empty-fragment",
        ),
    ],
)
def test_reject_non_exact_or_unsafe_instagram_story_urls(url: str) -> None:
    """Verify only an exact safe single-Story path passes validation."""
    with pytest.raises(AppError) as exc_info:
        validate_post_url(url)

    assert exc_info.value.code == "unsupported_url"


@pytest.mark.parametrize(
    ("url", "expected_message"),
    [
        pytest.param(
            "http://www.instagram.com/stories/example.user/1234567890/",
            "Only supported HTTPS media target URLs are accepted.",
            id="non-https-exact-story",
        ),
        pytest.param(
            "https://www.instagram.com/stories/highlights/1234567890/",
            "Only supported single media target URLs are accepted.",
            id="unsupported-story-path",
        ),
    ],
)
def test_story_validation_errors_use_media_target_messages(url: str, expected_message: str) -> None:
    """Verify Story validation errors describe media targets without post-only claims."""
    with pytest.raises(AppError) as exc_info:
        validate_post_url(url)

    assert exc_info.value.code == "unsupported_url"
    assert exc_info.value.message == expected_message


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/user/status/1",
        "https://x.com.evil.example/user/status/1",
        "https://x.com@evil.example/user/status/1",
        "https://x.com/user/status/1#fragment",
        "https://x.com:8443/user/status/1",
        "https://x.com/user",
        "https://x.com/search?q=cat",
        "https://www.instagram.com/user/",
    ],
)
def test_reject_unsafe_or_unsupported_urls(url: str) -> None:
    """Verify unsafe and unsupported URLs never become extraction inputs."""
    with pytest.raises(AppError) as exc_info:
        validate_post_url(url)

    assert exc_info.value.code == "unsupported_url"


def test_platform_redirect_must_end_at_supported_post() -> None:
    """Verify a platform redirect is accepted only when its target is a post."""
    parsed = validate_platform_redirect_target(
        "https://x.com/share",
        "https://x.com/user/status/123456789?utm_source=share",
    )

    assert parsed.platform == "x"
    assert parsed.target_id == "123456789"


def test_platform_redirect_cannot_leave_allowed_hosts() -> None:
    """Verify redirects to unrelated hosts are rejected."""
    with pytest.raises(AppError) as exc_info:
        validate_platform_redirect_target(
            "https://x.com/share",
            "https://evil.example/user/status/123456789",
        )

    assert exc_info.value.code == "unsupported_url"


def test_redirect_chain_rejects_excessive_hops() -> None:
    """Verify platform redirect resolution has a finite hop limit."""
    with pytest.raises(AppError) as exc_info:
        validate_redirect_chain(
            "https://x.com/share",
            [
                "https://x.com/share/1",
                "https://x.com/share/2",
                "https://x.com/user/status/123456789",
            ],
            max_redirects=2,
        )

    assert exc_info.value.code == "unsupported_url"


def test_empty_redirect_chain_uses_media_target_message() -> None:
    """Verify an empty redirect chain reports a platform-neutral target error."""
    with pytest.raises(AppError) as exc_info:
        validate_redirect_chain(
            "https://x.com/share",
            [],
            max_redirects=2,
        )

    assert exc_info.value.code == "unsupported_url"
    assert exc_info.value.message == ("The URL did not resolve to a supported media target.")
