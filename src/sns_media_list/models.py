"""Public response and private media record models."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

Platform = Literal["instagram", "x"]
MediaType = Literal["image", "video"]
TokenPurpose = Literal["preview", "download"]


class MediaItem(BaseModel):
    """Describe one downloadable media item without private source details."""

    token: str
    media_type: MediaType
    filename: str
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0)
    preview_url: str | None = None
    download_url: str


class ExtractionResponse(BaseModel):
    """Describe a normalized public-post extraction response."""

    platform: Platform
    post_url: HttpUrl
    author: str | None = None
    description: str | None = None
    unavailable_media_count: int = Field(default=0, ge=0)
    media: list[MediaItem]


class ErrorResponse(BaseModel):
    """Describe a stable API error response."""

    code: str
    message: str
    request_id: str


@dataclass(frozen=True, slots=True)
class PrivateMediaRecord:
    """Store upstream details that must never be serialized to the client."""

    token: str
    purpose: TokenPurpose
    source_url: str
    media_class: MediaType
    filename: str
    platform: Platform
    expires_at: float
    request_headers: Mapping[str, str]
    content_type: str | None = None
