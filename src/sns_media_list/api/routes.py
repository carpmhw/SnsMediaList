"""Extraction and media download API routes."""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from time import perf_counter

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..api.limits import Lease, RequestLimiter, client_identity
from ..errors import AppError
from ..logging_config import build_event
from ..models import ExtractionResponse, PrivateMediaRecord
from ..network.media_client import (
    MediaClient,
    MediaResponse,
    build_forward_response_headers,
    build_preview_headers,
    iter_validated_body,
)
from ..services.extraction_service import ExtractionService
from ..services.thumbnail import ThumbnailGenerator, validate_thumbnail_media_class
from ..services.thumbnail_cache import ThumbnailCoordinator

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
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
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
    async def preview_media(request: Request, token: str) -> Response:
        """Proxy or generate one authorized preview with passive browser headers."""
        record = service.token_store.get(token, "preview")
        lease = limiter.acquire_download(
            client_identity(
                request.client.host if request.client else "unknown",
                request.headers.get("x-forwarded-for"),
                trusted_proxy_cidrs,
            )
        )
        if record.preview_mode == "generated":
            return await _generate_preview(
                record,
                media_client,
                thumbnail_generator,
                thumbnail_coordinator,
                lease=lease,
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
    except BaseException:
        await lease.release()
        raise
    iterator = iter_validated_body(
        response,
        preview=preview,
        expected_media_class=record.media_class,
    )
    try:
        first_chunk = await anext(iterator)
    except StopAsyncIteration as error:
        try:
            await response.close()
        finally:
            await lease.release()
        raise AppError("upstream_media_invalid", "The upstream media body was empty.") from error
    except BaseException:
        try:
            await response.close()
        finally:
            await lease.release()
        raise

    async def body() -> AsyncIterator[bytes]:
        """Yield the prevalidated first chunk and remaining upstream bytes."""
        try:
            yield first_chunk
            async for chunk in iterator:
                yield chunk
        finally:
            try:
                await response.close()
            finally:
                await lease.release()

    headers = build_forward_response_headers(response, filename=filename, preview=preview)
    return StreamingResponse(
        body(), headers=headers, media_type=response.headers.get("content-type")
    )


async def _generate_preview(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
    *,
    lease: Lease,
) -> Response:
    """Generate or retrieve one buffered JPEG and release the request lease."""

    async def generate() -> bytes:
        """Fetch the source and generate a JPEG within the complete preview deadline."""

        async def fetch_and_generate() -> bytes:
            """Fetch, validate, generate, and close one upstream response."""
            response: MediaResponse | None = None
            try:
                response = await media_client.fetch(
                    record.source_url, headers=record.request_headers
                )
                if not 200 <= response.status_code < 300:
                    raise AppError(
                        "upstream_media_invalid", "The upstream media response was not successful."
                    )
                validate_thumbnail_media_class(
                    response.headers.get("content-type", ""), record.media_class
                )
                return await thumbnail_generator.generate(response)
            except AppError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                ) from error
            finally:
                if response is not None:
                    await _close_response_with_timeout(response, timeout=timeout)

        timeout = getattr(thumbnail_generator, "timeout_seconds", None)
        if timeout is None:
            return await fetch_and_generate()
        try:
            return await asyncio.wait_for(fetch_and_generate(), timeout)
        except TimeoutError as error:
            raise AppError("upstream_media_invalid", "Thumbnail generation timed out.") from error

    try:
        data = await thumbnail_coordinator.get_or_generate(
            record.token,
            expires_at=record.expires_at,
            factory=generate,
        )
    finally:
        await lease.release()
    headers = build_preview_headers(record.filename)
    headers["Content-Type"] = "image/jpeg"
    headers["Content-Length"] = str(len(data))
    return Response(content=data, headers=headers, media_type="image/jpeg")


async def _close_response_with_timeout(
    response: MediaResponse,
    *,
    timeout: float | None,
) -> None:
    """Close a generated-preview source without exceeding its cleanup deadline."""
    cleanup_timeout = min(timeout if timeout is not None else 1.0, 1.0)
    task = asyncio.create_task(response.close())
    done, _pending = await asyncio.wait({task}, timeout=cleanup_timeout)
    if done:
        await asyncio.gather(task, return_exceptions=True)
    else:
        task.cancel()
