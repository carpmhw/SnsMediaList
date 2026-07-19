"""Extraction and media download API routes."""

import logging
from collections.abc import AsyncIterator, Iterable
from time import perf_counter

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..api.limits import Lease, RequestLimiter, client_identity
from ..errors import AppError
from ..logging_config import build_event
from ..models import ExtractionResponse, PrivateMediaRecord
from ..network.media_client import (
    MediaClient,
    build_forward_response_headers,
    iter_validated_body,
)
from ..services.extraction_service import ExtractionService

logger = logging.getLogger("sns_media_list")


class ExtractionRequest(BaseModel):
    """Represent the only input accepted by the extraction endpoint."""

    url: str = Field(min_length=1, max_length=2048)


def build_router(
    service: ExtractionService,
    media_client: MediaClient,
    *,
    limiter: RequestLimiter,
    trusted_proxy_cidrs: Iterable[str],
) -> APIRouter:
    """Create API routes with injected service dependencies."""
    router = APIRouter(prefix="/api")

    @router.post("/extractions")
    async def create_extraction(request: Request, payload: ExtractionRequest) -> ExtractionResponse:
        """Extract a public post and return its normalized media list."""
        lease = limiter.acquire_extraction(
            client_identity(
                request.client.host if request.client else "unknown",
                request.headers.get("x-forwarded-for"),
                trusted_proxy_cidrs,
            )
        )
        async with lease:
            started = perf_counter()
            result = await service.extract(payload.url)
        logger.info(
            "extraction_complete",
            extra={
                "event": build_event(
                    request_id=getattr(request.state, "request_id", "unknown"),
                    platform=result.platform,
                    outcome="success",
                    duration_ms=(perf_counter() - started) * 1000,
                    item_count=len(result.media),
                )
            },
        )
        return result

    @router.get("/media/{token}/download")
    async def download_media(request: Request, token: str) -> StreamingResponse:
        """Stream one authorized media resource as an attachment."""
        record = service.token_store.get(token, "download")
        lease = limiter.acquire_download(
            client_identity(
                request.client.host if request.client else "unknown",
                request.headers.get("x-forwarded-for"),
                trusted_proxy_cidrs,
            )
        )
        return await _stream_media(record, media_client, preview=False, lease=lease)

    @router.get("/media/{token}/preview")
    async def preview_media(request: Request, token: str) -> StreamingResponse:
        """Stream one authorized raster preview with passive browser headers."""
        record = service.token_store.get(token, "preview")
        lease = limiter.acquire_download(
            client_identity(
                request.client.host if request.client else "unknown",
                request.headers.get("x-forwarded-for"),
                trusted_proxy_cidrs,
            )
        )
        return await _stream_media(record, media_client, preview=True, lease=lease)

    return router


async def _stream_media(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    *,
    preview: bool,
    lease: Lease,
) -> StreamingResponse:
    """Fetch, prevalidate, and stream one private media record."""
    source_url = record.source_url
    request_headers = record.request_headers
    filename = record.filename
    try:
        response = await media_client.fetch(source_url, headers=request_headers)
    except Exception:
        await lease.release()
        raise
    iterator = iter_validated_body(response, preview=preview)
    try:
        first_chunk = await anext(iterator)
    except StopAsyncIteration as error:
        await response.close()
        await lease.release()
        raise AppError("upstream_media_invalid", "The upstream media body was empty.") from error
    except Exception:
        await response.close()
        await lease.release()
        raise

    async def body() -> AsyncIterator[bytes]:
        """Yield the prevalidated first chunk and remaining upstream bytes."""
        try:
            yield first_chunk
            async for chunk in iterator:
                yield chunk
        finally:
            await response.close()
            await lease.release()

    headers = build_forward_response_headers(response, filename=filename, preview=preview)
    return StreamingResponse(
        body(), headers=headers, media_type=response.headers.get("content-type")
    )
