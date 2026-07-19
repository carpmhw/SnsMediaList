"""Tests for supported post URL validation."""

import pytest

from sns_media_list.errors import AppError
from sns_media_list.url_validation import (
    validate_platform_redirect_target,
    validate_post_url,
    validate_redirect_chain,
)


@pytest.mark.parametrize(
    ("url", "platform", "post_id"),
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
def test_supported_public_post_urls(url: str, platform: str, post_id: str) -> None:
    """Verify accepted URL forms return platform and post identity."""
    parsed = validate_post_url(url)

    assert parsed.platform == platform
    assert parsed.post_id == post_id
    assert "ref=share" not in parsed.canonical_url


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
        "https://www.instagram.com/stories/user/1/",
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
    assert parsed.post_id == "123456789"


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
