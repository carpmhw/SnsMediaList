"""Tests for immediate bounded concurrency and client identity."""

import pytest

from sns_media_list.api.limits import RequestLimiter, client_identity
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


def test_untrusted_forwarded_header_is_ignored() -> None:
    """Verify direct clients cannot spoof their rate-limit identity."""
    assert client_identity("198.51.100.10", "203.0.113.10", ()) == "198.51.100.10"


def test_trusted_proxy_forwarded_header_is_used() -> None:
    """Verify an explicitly trusted proxy may provide the client identity."""
    assert (
        client_identity("10.0.0.10", "203.0.113.10, 10.0.0.11", ("10.0.0.0/8",)) == "203.0.113.10"
    )
