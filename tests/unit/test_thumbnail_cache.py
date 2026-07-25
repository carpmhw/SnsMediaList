"""Tests for byte-bounded thumbnail result caching."""

import asyncio

import pytest

from sns_media_list.errors import AppError
from sns_media_list.services.thumbnail_cache import ThumbnailCache, ThumbnailCoordinator


class FakeClock:
    """Provide deterministic time for cache expiry tests."""

    def __init__(self, value: float = 100.0) -> None:
        """Store the initial monotonic value."""
        self.value = value

    def __call__(self) -> float:
        """Return the configured monotonic value."""
        return self.value


def test_cache_reuses_success_and_tracks_bytes() -> None:
    """Verify successful thumbnails can be retrieved before their expiry."""
    cache = ThumbnailCache(max_bytes=10, clock=FakeClock())

    cache.put_success("one", b"12345", expires_at=200.0)

    assert cache.get("one") == b"12345"
    assert cache.size_bytes == 5


def test_cache_evicts_least_recently_used_entry() -> None:
    """Verify byte pressure evicts the least recently used thumbnail."""
    cache = ThumbnailCache(max_bytes=10, clock=FakeClock())
    cache.put_success("one", b"12345", expires_at=200.0)
    cache.put_success("two", b"67890", expires_at=200.0)
    assert cache.get("one") == b"12345"

    cache.put_success("three", b"abcde", expires_at=200.0)

    assert cache.get("one") == b"12345"
    assert cache.get("two") is None
    assert cache.get("three") == b"abcde"


def test_cache_expiry_removes_success_and_negative_entries() -> None:
    """Verify success and deterministic failure entries expire at token expiry."""
    clock = FakeClock()
    cache = ThumbnailCache(max_bytes=10, clock=clock)
    cache.put_success("one", b"123", expires_at=101.0)
    cache.put_failure("bad", expires_at=101.0)
    clock.value = 101.0

    assert cache.get("one") is None
    assert cache.get("bad") is None
    assert cache.size_bytes == 0


def test_cache_does_not_store_entry_larger_than_capacity() -> None:
    """Verify an oversized derived result can pass through without consuming cache space."""
    cache = ThumbnailCache(max_bytes=3, clock=FakeClock())

    cache.put_success("large", b"1234", expires_at=200.0)

    assert cache.get("large") is None
    assert cache.size_bytes == 0


def test_cache_negative_entry_raises_stable_error() -> None:
    """Verify deterministic failures are cached without retaining private details."""
    cache = ThumbnailCache(max_bytes=10, clock=FakeClock())
    cache.put_failure("bad", expires_at=200.0)

    with pytest.raises(AppError) as exc_info:
        cache.get("bad")

    assert exc_info.value.code == "upstream_media_invalid"


def test_cache_bounds_negative_entries_by_count() -> None:
    """Verify negative entries cannot accumulate beyond the metadata bound."""
    cache = ThumbnailCache(max_bytes=10, max_negative_entries=2, clock=FakeClock())
    cache.put_success("one", b"12345", expires_at=200.0)
    cache.put_success("two", b"67890", expires_at=200.0)
    cache.put_failure("bad-one", expires_at=200.0)
    cache.put_failure("bad-two", expires_at=200.0)
    cache.put_failure("bad-three", expires_at=200.0)

    assert cache.get("one") == b"12345"
    assert cache.get("two") == b"67890"
    assert cache.get("bad-one") is None
    with pytest.raises(AppError):
        cache.get("bad-two")
    with pytest.raises(AppError):
        cache.get("bad-three")


def test_cache_byte_eviction_preserves_negative_entries() -> None:
    """Verify JPEG byte pressure evicts successes without discarding negative metadata."""
    cache = ThumbnailCache(max_bytes=10, max_negative_entries=2, clock=FakeClock())
    cache.put_failure("bad", expires_at=200.0)
    cache.put_success("one", b"1234567890", expires_at=200.0)
    cache.put_success("two", b"abcde", expires_at=200.0)

    with pytest.raises(AppError):
        cache.get("bad")
    assert cache.get("one") is None
    assert cache.get("two") == b"abcde"


@pytest.mark.asyncio
async def test_coordinator_shares_one_generation_for_same_token() -> None:
    """Verify duplicate requests await one generation operation."""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=100, clock=FakeClock()), max_concurrency=1
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def generate() -> bytes:
        """Block one fake generation until both callers are attached."""
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"jpeg"

    first = asyncio.create_task(
        coordinator.get_or_generate("same", expires_at=200.0, factory=generate)
    )
    await started.wait()
    second = asyncio.create_task(
        coordinator.get_or_generate("same", expires_at=200.0, factory=generate)
    )
    release.set()

    assert await first == b"jpeg"
    assert await second == b"jpeg"
    assert calls == 1


@pytest.mark.asyncio
async def test_coordinator_rejects_different_token_when_slot_is_busy() -> None:
    """Verify a new token is rejected instead of queued behind another generation."""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=100, clock=FakeClock()), max_concurrency=1
    )
    release = asyncio.Event()

    async def generate() -> bytes:
        """Hold the only generation slot."""
        await release.wait()
        return b"jpeg"

    first = asyncio.create_task(
        coordinator.get_or_generate("one", expires_at=200.0, factory=generate)
    )
    await asyncio.sleep(0)

    with pytest.raises(AppError) as exc_info:
        await coordinator.get_or_generate("two", expires_at=200.0, factory=generate)

    assert exc_info.value.code == "local_rate_limited"
    release.set()
    assert await first == b"jpeg"


@pytest.mark.asyncio
async def test_coordinator_caches_deterministic_failure() -> None:
    """Verify deterministic generation failure avoids repeated work."""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=100, clock=FakeClock()), max_concurrency=1
    )
    calls = 0

    async def generate() -> bytes:
        """Raise a safe deterministic generation failure."""
        nonlocal calls
        calls += 1
        error = AppError("upstream_media_invalid", "safe failure")
        error.deterministic = True
        raise error

    for _ in range(2):
        with pytest.raises(AppError) as exc_info:
            await coordinator.get_or_generate("bad", expires_at=200.0, factory=generate)
        assert exc_info.value.code == "upstream_media_invalid"

    assert calls == 1


@pytest.mark.asyncio
async def test_coordinator_cancellation_does_not_release_running_slot_early() -> None:
    """Verify cancelling the owner cancels generation before another slot is admitted."""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=100, clock=FakeClock()), max_concurrency=1
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def generate() -> bytes:
        """Keep generation active until cancellation reaches the factory."""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return b"jpeg"

    owner = asyncio.create_task(
        coordinator.get_or_generate("one", expires_at=200.0, factory=generate)
    )
    await started.wait()
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    await cancelled.wait()

    async def second_generate() -> bytes:
        """Return a result after the cancelled work has cleaned up."""
        return b"jpeg"

    assert (
        await coordinator.get_or_generate("two", expires_at=200.0, factory=second_generate)
        == b"jpeg"
    )


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_generation() -> None:
    """Verify a cancelled duplicate waiter leaves the owner generation running."""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=100, clock=FakeClock()), max_concurrency=1
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def generate() -> bytes:
        """Hold shared work until the owner releases it."""
        started.set()
        await release.wait()
        return b"jpeg"

    owner = asyncio.create_task(
        coordinator.get_or_generate("same", expires_at=200.0, factory=generate)
    )
    await started.wait()
    waiter = asyncio.create_task(
        coordinator.get_or_generate("same", expires_at=200.0, factory=generate)
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    assert await owner == b"jpeg"
