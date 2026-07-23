"""Process-local byte-bounded cache and single-flight coordination."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ..errors import AppError


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """Store one generated result or safe deterministic failure."""

    data: bytes | None
    expires_at: float
    failed: bool = False


class ThumbnailCache:
    """Store generated JPEGs and safe negative results under a byte limit."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_negative_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty LRU cache with deterministic clock injection."""
        self.max_bytes = max_bytes
        self.max_negative_entries = max_negative_entries
        self.clock = clock
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._size_bytes = 0
        self._failure_count = 0

    @property
    def size_bytes(self) -> int:
        """Return the number of successful thumbnail bytes currently retained."""
        return self._size_bytes

    def get(self, token: str) -> bytes | None:
        """Return cached bytes, raise a safe cached failure, or return a miss."""
        entry = self._entries.get(token)
        if entry is None:
            return None
        if self.clock() >= entry.expires_at:
            self._remove(token)
            return None
        self._entries.move_to_end(token)
        if entry.failed:
            raise AppError("upstream_media_invalid", "The generated thumbnail is unavailable.")
        return entry.data

    def put_success(self, token: str, data: bytes, *, expires_at: float) -> None:
        """Cache one successful JPEG unless it is larger than the whole cache."""
        self._remove(token)
        if len(data) > self.max_bytes or expires_at <= self.clock():
            return
        while self._size_bytes + len(data) > self.max_bytes and self._entries:
            if not self._evict_oldest_success():
                return
        self._entries[token] = _CacheEntry(data=data, expires_at=expires_at)
        self._size_bytes += len(data)

    def put_failure(self, token: str, *, expires_at: float) -> None:
        """Cache a deterministic safe failure until the token expiry."""
        self._remove(token)
        if expires_at > self.clock() and self.max_negative_entries > 0:
            while self._failure_count >= self.max_negative_entries:
                if not self._evict_oldest_failure():
                    break
            self._entries[token] = _CacheEntry(
                data=None,
                expires_at=expires_at,
                failed=True,
            )
            self._failure_count += 1

    def _evict_oldest_success(self) -> bool:
        """Evict the least-recently-used generated JPEG under byte pressure."""
        for token, entry in self._entries.items():
            if entry.data is not None:
                self._remove(token)
                return True
        return False

    def _evict_oldest_failure(self) -> bool:
        """Evict the least-recently-used negative entry without touching successes."""
        for token, entry in self._entries.items():
            if entry.failed:
                self._remove(token)
                return True
        return False

    def _remove(self, token: str) -> None:
        """Remove one entry and update the successful-byte total."""
        entry = self._entries.pop(token, None)
        if entry is None:
            return
        if entry.failed:
            self._failure_count -= 1
        elif entry.data is not None:
            self._size_bytes -= len(entry.data)


class ThumbnailCoordinator:
    """Coordinate cached thumbnail generation without creating a work queue."""

    def __init__(self, cache: ThumbnailCache, *, max_concurrency: int) -> None:
        """Initialize the cache-backed generation coordinator."""
        self.cache = cache
        self.max_concurrency = max_concurrency
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[bytes]] = {}
        self._active = 0

    async def get_or_generate(
        self,
        token: str,
        *,
        expires_at: float,
        factory: Callable[[], Coroutine[Any, Any, bytes]],
    ) -> bytes:
        """Return a cached result or share one bounded generation operation."""
        cached = self.cache.get(token)
        if cached is not None:
            return cached

        owner = False
        async with self._lock:
            cached = self.cache.get(token)
            if cached is not None:
                return cached
            task = self._inflight.get(token)
            if task is None:
                if self._active >= self.max_concurrency:
                    raise AppError(
                        "local_rate_limited",
                        "Too many thumbnail generations are active.",
                        retry_after=1,
                    )
                task = asyncio.create_task(factory())
                self._inflight[token] = task
                self._active += 1
                owner = True

        try:
            result = await task if owner else await asyncio.shield(task)
            if owner:
                self.cache.put_success(token, result, expires_at=expires_at)
            return result
        except AppError as error:
            if owner and getattr(error, "deterministic", False):
                self.cache.put_failure(token, expires_at=expires_at)
            raise
        finally:
            if owner:
                async with self._lock:
                    self._inflight.pop(token, None)
                    self._active = max(0, self._active - 1)
