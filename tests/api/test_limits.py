"""Tests for immediate bounded concurrency and client identity."""

import pytest

from sns_media_list.api.limits import AttemptLimiter, RequestLimiter, client_identity
from sns_media_list.errors import AppError


@pytest.mark.asyncio
async def test_same_client_cannot_start_two_extractions() -> None:
    """Verify one socket client has at most one active extraction."""
    limiter = RequestLimiter(max_extractions=2, max_downloads=1)

    lease = limiter.acquire_extraction("203.0.113.10")
    with pytest.raises(AppError) as exc_info:
        limiter.acquire_extraction("203.0.113.10")

    assert exc_info.value.code == "local_rate_limited"
    await lease.release()
    await limiter.acquire_extraction("203.0.113.10").release()


def test_process_wide_limit_rejects_without_queue() -> None:
    """Verify process-wide extraction capacity rejects immediately."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    _lease = limiter.acquire_extraction("203.0.113.10")

    with pytest.raises(AppError) as exc_info:
        limiter.acquire_extraction("203.0.113.11")

    assert exc_info.value.code == "local_rate_limited"
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_per_client_download_limit_rejects_without_blocking_other_clients() -> None:
    """Verify one client cannot occupy every download lease."""
    limiter = RequestLimiter(
        max_extractions=1,
        max_downloads=2,
        max_downloads_per_client=1,
    )

    first = limiter.acquire_download("203.0.113.10")
    with pytest.raises(AppError) as exc_info:
        limiter.acquire_download("203.0.113.10")
    second = limiter.acquire_download("203.0.113.11")

    assert exc_info.value.code == "local_rate_limited"
    await first.release()
    await second.release()


def test_untrusted_forwarded_header_is_ignored() -> None:
    """Verify direct clients cannot spoof their rate-limit identity."""
    assert client_identity("198.51.100.10", "203.0.113.10", ()) == "198.51.100.10"


def test_trusted_proxy_forwarded_header_is_used() -> None:
    """Verify an explicitly trusted proxy may provide the client identity."""
    assert (
        client_identity("10.0.0.10", "203.0.113.10, 10.0.0.11", ("10.0.0.0/8",)) == "203.0.113.10"
    )


def test_invalid_trusted_proxy_cidr_falls_back_to_socket_identity() -> None:
    """Verify a malformed proxy list cannot crash identity resolution or enable spoofing."""
    assert client_identity("10.0.0.10", "203.0.113.10", ("not-a-cidr",)) == "10.0.0.10"


def test_attempt_limiter_rejects_the_next_extraction_with_retry_after() -> None:
    """Verify a client's rolling extraction window rejects excess attempts immediately."""
    now = [100.0]
    limiter = AttemptLimiter(
        extraction_limit=2,
        media_limit=3,
        window_seconds=60.0,
        max_identities=4,
        clock=lambda: now[0],
    )

    limiter.acquire("extraction", "203.0.113.10")
    limiter.acquire("extraction", "203.0.113.10")
    with pytest.raises(AppError) as exc_info:
        limiter.acquire("extraction", "203.0.113.10")

    assert exc_info.value.code == "local_rate_limited"
    assert exc_info.value.retry_after == 60


def test_attempt_limiter_rejects_unbounded_configuration() -> None:
    """Verify the limiter cannot be constructed beyond bounded memory limits."""
    with pytest.raises(ValueError):
        AttemptLimiter(
            extraction_limit=10,
            media_limit=120,
            window_seconds=60.0,
            max_identities=2_049,
        )


def test_attempt_limiter_prunes_expired_attempts_and_separates_media() -> None:
    """Verify rolling expiry and extraction/media buckets are independent."""
    now = [10.0]
    limiter = AttemptLimiter(
        extraction_limit=1,
        media_limit=1,
        window_seconds=10.0,
        max_identities=4,
        clock=lambda: now[0],
    )

    limiter.acquire("extraction", "203.0.113.10")
    limiter.acquire("media", "203.0.113.10")
    with pytest.raises(AppError):
        limiter.acquire("extraction", "203.0.113.10")
    now[0] = 20.1
    limiter.acquire("extraction", "203.0.113.10")
    assert limiter.identity_count == 1


def test_attempt_limiter_evicts_oldest_identity_at_capacity() -> None:
    """Verify the attempt table remains bounded when new identities churn."""
    now = [0.0]
    limiter = AttemptLimiter(
        extraction_limit=2,
        media_limit=2,
        window_seconds=60.0,
        max_identities=2,
        clock=lambda: now[0],
    )

    limiter.acquire("extraction", "203.0.113.10")
    now[0] = 1.0
    limiter.acquire("extraction", "203.0.113.11")
    now[0] = 2.0
    limiter.acquire("extraction", "203.0.113.12")

    assert limiter.identity_count == 2
    limiter.acquire("extraction", "203.0.113.10")
