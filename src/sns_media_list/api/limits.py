"""Immediate in-process concurrency limits and trusted-proxy identity."""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

from ..errors import AppError


class RequestLimiter:
    """Reject work immediately when process or client limits are exhausted."""

    def __init__(self, *, max_extractions: int, max_downloads: int) -> None:
        """Initialize extraction and download counters."""
        self.max_extractions = max_extractions
        self.max_downloads = max_downloads
        self._active_extractions: set[str] = set()
        self._active_downloads = 0

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
        if self._active_downloads >= self.max_downloads:
            raise AppError("local_rate_limited", "Too many downloads are active.", retry_after=1)
        self._active_downloads += 1
        return Lease(self, "download", client_ip)

    def release(self, kind: str, client_ip: str) -> None:
        """Release one previously reserved slot."""
        if kind == "extraction":
            self._active_extractions.discard(client_ip)
        elif kind == "download":
            self._active_downloads = max(0, self._active_downloads - 1)


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
    trusted = any(peer in ipaddress.ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs)
    if not trusted or not forwarded_for:
        return str(peer)
    candidate = forwarded_for.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return str(peer)
