"""Immediate in-process concurrency limits and trusted-proxy identity."""

import ipaddress
import math
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from ..errors import AppError

AttemptKind = Literal["extraction", "media"]

_MAX_EXTRACTION_ATTEMPTS = 10
_MAX_MEDIA_ATTEMPTS = 120
_MAX_IDENTITIES = 2_048


@dataclass(slots=True)
class _AttemptBucket:
    """Store rolling attempt timestamps for one client identity."""

    extraction: deque[float]
    media: deque[float]


class AttemptLimiter:
    """Enforce bounded rolling request-attempt windows per client identity."""

    def __init__(
        self,
        *,
        extraction_limit: int,
        media_limit: int,
        window_seconds: float,
        max_identities: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize rolling limits with an injectable monotonic clock."""
        if (
            extraction_limit <= 0
            or extraction_limit > _MAX_EXTRACTION_ATTEMPTS
            or media_limit <= 0
            or media_limit > _MAX_MEDIA_ATTEMPTS
            or max_identities <= 0
            or max_identities > _MAX_IDENTITIES
        ):
            raise ValueError("attempt limiter configuration exceeds bounded limits")
        self.extraction_limit = extraction_limit
        self.media_limit = media_limit
        self.window_seconds = window_seconds
        self.max_identities = max_identities
        self._clock = clock or monotonic
        self._attempts: OrderedDict[str, _AttemptBucket] = OrderedDict()

    @property
    def identity_count(self) -> int:
        """Return the number of client identities retained by the limiter."""
        return len(self._attempts)

    def acquire(self, kind: AttemptKind, client_ip: str) -> None:
        """Record one attempt or raise an immediate rolling-window error."""
        now = self._clock()
        self._purge_expired(now)
        bucket = self._attempts.get(client_ip)
        if bucket is None:
            if len(self._attempts) >= self.max_identities:
                self._attempts.popitem(last=False)
            bucket = _AttemptBucket(extraction=deque(), media=deque())
            self._attempts[client_ip] = bucket
        else:
            self._attempts.move_to_end(client_ip)
        attempts = bucket.extraction if kind == "extraction" else bucket.media
        self._prune_attempts(attempts, now)
        limit = self.extraction_limit if kind == "extraction" else self.media_limit
        if len(attempts) >= limit:
            retry_after = max(1, math.ceil(attempts[0] + self.window_seconds - now))
            raise AppError(
                "local_rate_limited",
                "Too many requests were attempted recently.",
                retry_after=retry_after,
            )
        attempts.append(now)

    def _purge_expired(self, now: float) -> None:
        """Remove identities whose extraction and media timestamps all expired."""
        for client_ip, bucket in list(self._attempts.items()):
            self._prune_attempts(bucket.extraction, now)
            self._prune_attempts(bucket.media, now)
            if not bucket.extraction and not bucket.media:
                del self._attempts[client_ip]

    def _prune_attempts(self, attempts: deque[float], now: float) -> None:
        """Discard timestamps outside the configured rolling window."""
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()


class RequestLimiter:
    """Reject work immediately when process or client limits are exhausted."""

    def __init__(
        self,
        *,
        max_extractions: int,
        max_downloads: int,
        max_downloads_per_client: int | None = None,
    ) -> None:
        """Initialize extraction and download counters."""
        self.max_extractions = max_extractions
        self.max_downloads = max_downloads
        self.max_downloads_per_client = (
            max_downloads if max_downloads_per_client is None else max_downloads_per_client
        )
        self._active_extractions: set[str] = set()
        self._active_downloads = 0
        self._active_downloads_by_client: dict[str, int] = {}

    def acquire_extraction(self, client_ip: str) -> "Lease":
        """Reserve one extraction slot or raise an immediate rate-limit error."""
        if (
            client_ip in self._active_extractions
            or len(self._active_extractions) >= self.max_extractions
        ):
            raise AppError(
                "local_rate_limited", "Too many extraction requests are active.", retry_after=1
            )
        self._active_extractions.add(client_ip)
        return Lease(self, "extraction", client_ip)

    def acquire_download(self, client_ip: str) -> "Lease":
        """Reserve one download slot or raise an immediate rate-limit error."""
        if (
            self._active_downloads >= self.max_downloads
            or self._active_downloads_by_client.get(client_ip, 0) >= self.max_downloads_per_client
        ):
            raise AppError("local_rate_limited", "Too many downloads are active.", retry_after=1)
        self._active_downloads += 1
        self._active_downloads_by_client[client_ip] = (
            self._active_downloads_by_client.get(client_ip, 0) + 1
        )
        return Lease(self, "download", client_ip)

    def release(self, kind: str, client_ip: str) -> None:
        """Release one previously reserved slot."""
        if kind == "extraction":
            self._active_extractions.discard(client_ip)
        elif kind == "download":
            self._active_downloads = max(0, self._active_downloads - 1)
            active_for_client = self._active_downloads_by_client.get(client_ip, 0) - 1
            if active_for_client > 0:
                self._active_downloads_by_client[client_ip] = active_for_client
            else:
                self._active_downloads_by_client.pop(client_ip, None)


@dataclass
class Lease:
    """Represent an idempotently releasable limiter reservation."""

    limiter: RequestLimiter
    kind: str
    client_ip: str
    released: bool = False

    async def release(self) -> None:
        """Release the reservation once."""
        if not self.released:
            self.limiter.release(self.kind, self.client_ip)
            self.released = True

    async def __aenter__(self) -> "Lease":
        """Return this lease for async context manager use."""
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Release the reservation when leaving an async context."""
        await self.release()


def client_identity(
    peer_ip: str,
    forwarded_for: str | None,
    trusted_proxy_cidrs: Iterable[str],
) -> str:
    """Return the socket peer or a validated forwarded client IP."""
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return peer_ip
    trusted = False
    for cidr in trusted_proxy_cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if network.prefixlen != 0 and peer in network:
            trusted = True
            break
    if not trusted or not forwarded_for:
        return str(peer)
    candidate = forwarded_for.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return str(peer)
