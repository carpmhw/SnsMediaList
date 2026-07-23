"""Tests for bounded, purpose-bound in-memory media tokens."""

import pytest

from sns_media_list.errors import AppError
from sns_media_list.security.tokens import MediaTokenDraft, TokenStore


class FakeClock:
    """Provide deterministic time for token expiry tests."""

    def __init__(self, value: float = 100.0) -> None:
        """Initialize the fake monotonic clock."""
        self.value = value

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.value


def draft(purpose: str = "download") -> MediaTokenDraft:
    """Build one valid private media token draft."""
    return MediaTokenDraft(
        purpose=purpose,
        source_url="https://pbs.twimg.com/media/1.jpg?name=orig",
        media_class="image",
        filename="x.jpg",
        platform="x",
        request_headers={"User-Agent": "test"},
    )


def preview_draft(mode: str = "generated") -> MediaTokenDraft:
    """Build a preview draft with an explicit proxy or generated mode."""
    return MediaTokenDraft(
        purpose="preview",
        source_url="https://video.twimg.com/video.mp4",
        media_class="video",
        filename="x.mp4",
        platform="x",
        request_headers={"User-Agent": "test"},
        preview_mode=mode,
    )


def test_token_is_opaque_and_bound_to_purpose() -> None:
    """Verify issued token records are private and purpose-bound."""
    clock = FakeClock()
    store = TokenStore(capacity=2, ttl_seconds=600, clock=clock)
    record = store.reserve([draft()])[0]

    assert len(record.token) >= 32
    assert store.get(record.token, "download") == record
    with pytest.raises(AppError) as exc_info:
        store.get(record.token, "preview")
    assert exc_info.value.code == "token_not_found"


def test_present_expired_token_returns_expired() -> None:
    """Verify a token remains distinguishable until cleanup runs."""
    clock = FakeClock()
    store = TokenStore(capacity=2, ttl_seconds=600, clock=clock)
    token = store.reserve([draft()])[0].token
    clock.value = 701.0

    with pytest.raises(AppError) as exc_info:
        store.get(token, "download")

    assert exc_info.value.code == "token_expired"


def test_cleaned_or_restart_token_returns_not_found() -> None:
    """Verify cleanup and a new process do not retain old token records."""
    clock = FakeClock()
    store = TokenStore(capacity=2, ttl_seconds=600, clock=clock)
    token = store.reserve([draft()])[0].token
    clock.value = 701.0
    store.purge_expired()

    with pytest.raises(AppError) as exc_info:
        store.get(token, "download")
    assert exc_info.value.code == "token_not_found"


def test_reservation_is_atomic_when_capacity_is_insufficient() -> None:
    """Verify a failed batch does not partially consume token capacity."""
    store = TokenStore(capacity=1, ttl_seconds=600, clock=FakeClock())

    with pytest.raises(AppError) as exc_info:
        store.reserve([draft(), draft()])

    assert exc_info.value.code == "capacity_exceeded"
    assert store.size == 0


def test_preview_mode_is_copied_to_private_record() -> None:
    """Verify token issuance preserves the selected preview execution mode."""
    record = TokenStore(capacity=2, ttl_seconds=600, clock=FakeClock()).reserve([preview_draft()])[
        0
    ]

    assert record.preview_mode == "generated"
