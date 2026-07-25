"""Application orchestration for extraction and token issuance."""

from typing import Protocol, cast

from pydantic import HttpUrl

from ..config import Settings
from ..extractor.normalizer import (
    build_media_request_headers,
    ensure_downloadable_media,
    normalize_gallery_output,
)
from ..models import ExtractionResponse, MediaItem, MediaType, Platform, PreviewMode
from ..security.tokens import MediaTokenDraft, TokenStore
from ..url_validation import ValidatedExtractionTarget, validate_post_url

LOCAL_PREVIEW_URL = "/placeholder.svg"


class Extractor(Protocol):
    """Define the async adapter interface used by the application service."""

    async def extract(self, target: ValidatedExtractionTarget) -> list[dict[str, object]]:
        """Extract raw metadata for one validated media target."""
        raise NotImplementedError


class ExtractionService:
    """Coordinate URL validation, extraction normalization, and token issuance."""

    def __init__(
        self, settings: Settings, *, extractor: Extractor, token_store: TokenStore
    ) -> None:
        """Initialize the service with injected extraction and token dependencies."""
        self.settings = settings
        self.extractor = extractor
        self.token_store = token_store

    async def extract(self, url: str) -> ExtractionResponse:
        """Extract one validated media target and atomically issue its media tokens."""
        validated = validate_post_url(url)
        raw_records = await self.extractor.extract(validated)
        normalized = ensure_downloadable_media(
            normalize_gallery_output(raw_records, media_limit=self.settings.media_limit)
        )

        drafts: list[MediaTokenDraft] = []
        preview_modes: list[PreviewMode | None] = []
        for item in normalized.items:
            headers = build_media_request_headers(item.platform)
            drafts.append(
                MediaTokenDraft(
                    purpose="download",
                    source_url=item.source_url,
                    media_class=cast(MediaType, item.media_type),
                    filename=item.filename,
                    platform=cast(Platform, item.platform),
                    request_headers=headers,
                )
            )
            preview_mode: PreviewMode | None
            if item.preview_source_url:
                preview_mode = "proxy"
            elif self.settings.generated_previews_enabled:
                preview_mode = "generated"
            else:
                preview_mode = None
            preview_modes.append(preview_mode)
            if preview_mode is not None:
                drafts.append(
                    MediaTokenDraft(
                        purpose="preview",
                        source_url=item.preview_source_url or item.source_url,
                        media_class=(
                            "image" if preview_mode == "proxy" else cast(MediaType, item.media_type)
                        ),
                        filename=item.filename,
                        platform=cast(Platform, item.platform),
                        request_headers=headers,
                        preview_mode=preview_mode,
                    )
                )
        records = self.token_store.reserve(drafts)
        record_index = 0
        media: list[MediaItem] = []
        for item, preview_mode in zip(normalized.items, preview_modes, strict=True):
            download_record = records[record_index]
            record_index += 1
            if preview_mode is None:
                preview_url = LOCAL_PREVIEW_URL
            else:
                preview_record = records[record_index]
                record_index += 1
                preview_url = f"/api/media/{preview_record.token}/preview"
            media.append(
                MediaItem(
                    token=download_record.token,
                    media_type=cast(MediaType, item.media_type),
                    filename=item.filename,
                    width=item.width,
                    height=item.height,
                    duration=item.duration,
                    preview_url=preview_url,
                    download_url=f"/api/media/{download_record.token}/download",
                )
            )
        return ExtractionResponse(
            platform=cast(Platform, normalized.platform),
            post_url=HttpUrl(normalized.post_url),
            author=normalized.author,
            description=normalized.description,
            unavailable_media_count=normalized.unavailable_media_count,
            media=media,
        )
