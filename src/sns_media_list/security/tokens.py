"""Bounded in-memory media token storage."""

import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ..errors import AppError
from ..models import MediaType, Platform, PreviewMode, PrivateMediaRecord, TokenPurpose


@dataclass(frozen=True, slots=True)
class MediaTokenDraft:
    """Describe private media data before a token is issued."""

    purpose: TokenPurpose
    source_url: str
    media_class: MediaType
    filename: str
    platform: Platform
    request_headers: Mapping[str, str]
    content_type: str | None = None
    preview_mode: PreviewMode = "proxy"


class TokenStore:
    """Store purpose-bound media records with TTL and atomic reservations."""

    def __init__(
        self,
        *,
        capacity: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty bounded token store."""
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._records: dict[str, PrivateMediaRecord] = {}

    @property
    def size(self) -> int:
        """Return the number of records currently retained."""
        return len(self._records)

    def reserve(self, drafts: Iterable[MediaTokenDraft]) -> list[PrivateMediaRecord]:
        """Atomically issue tokens for a complete batch or raise capacity error."""
        pending = list(drafts)
        self.purge_expired()
        if len(self._records) + len(pending) > self.capacity:
            raise AppError("capacity_exceeded", "Temporary media capacity is full.")

        expires_at = self.clock() + self.ttl_seconds
        records = [
            PrivateMediaRecord(
                token=secrets.token_urlsafe(32),
                purpose=draft.purpose,
                source_url=draft.source_url,
                media_class=draft.media_class,
                filename=draft.filename,
                platform=draft.platform,
                expires_at=expires_at,
                request_headers=dict(draft.request_headers),
                content_type=draft.content_type,
                preview_mode=draft.preview_mode,
            )
            for draft in pending
        ]
        self._records.update({record.token: record for record in records})
        return records

    def get(self, token: str, purpose: str) -> PrivateMediaRecord:
        """Return a valid record or a stable missing/expired token error."""
        record = self._records.get(token)
        if record is None or record.purpose != purpose:
            raise AppError("token_not_found", "The media token is not available.")
        if self.clock() > record.expires_at:
            raise AppError("token_expired", "The media token has expired.")
        return record

    def delete(self, token: str) -> None:
        """Remove one token without affecting other unexpired records."""
        self._records.pop(token, None)

    def purge_expired(self) -> None:
        """Remove records whose expiry deadline has passed."""
        now = self.clock()
        expired = [token for token, record in self._records.items() if record.expires_at < now]
        for token in expired:
            del self._records[token]
