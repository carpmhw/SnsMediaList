"""Extraction and media download API routes."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from time import monotonic, perf_counter
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

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


class DeadlineStreamingResponse(StreamingResponse):
    """Stream one response while bounding upstream and downstream lifetime."""

    def __init__(
        self,
        *args: Any,
        deadline: float | None,
        cleanup: Callable[[], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Store the absolute deadline alongside the standard streaming response."""
        super().__init__(*args, **kwargs)
        self.deadline = deadline
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Cancel a downstream response when its complete lifetime expires."""
        try:
            if self.deadline is None:
                await super().__call__(scope, receive, send)
            else:
                try:
                    async with asyncio.timeout_at(self.deadline):
                        await super().__call__(scope, receive, send)
                except TimeoutError:
                    return
        finally:
            await self._run_cleanup()

    async def _run_cleanup(self) -> None:
        """Run response cleanup once even when streaming never starts."""
        cleanup = self._cleanup
        self._cleanup = None
        if cleanup is not None:
            await cleanup()


class ExtractionRequest(BaseModel):
    """Represent the only input accepted by the extraction endpoint."""

    url: str = Field(min_length=1, max_length=2048)


def build_router(
    service: ExtractionService,
    media_client: MediaClient,
    *,
    limiter: RequestLimiter,
    trusted_proxy_cidrs: Iterable[str],
    media_response_timeout_seconds: float,
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
) -> APIRouter:
    """Create API routes with injected service dependencies."""
    router = APIRouter(prefix="/api")

    @router.post("/extractions")
    async def create_extraction(request: Request, payload: ExtractionRequest) -> ExtractionResponse:
        """Extract a public post and return its normalized media list."""
        lease = limiter.acquire_extraction(_request_client_identity(request, trusted_proxy_cidrs))
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
        lease = limiter.acquire_download(_request_client_identity(request, trusted_proxy_cidrs))
        return await _stream_media(
            record,
            media_client,
            preview=False,
            lease=lease,
            response_timeout=media_response_timeout_seconds,
        )

    @router.head("/media/{token}/download")
    async def preflight_download(token: str) -> Response:
        """Validate a download token without contacting or reading upstream media."""
        service.token_store.get(token, "download")
        return Response(status_code=204)

    @router.get("/media/{token}/preview")
    async def preview_media(request: Request, token: str) -> Response:
        """Proxy or generate one authorized preview with passive browser headers."""
        record = service.token_store.get(token, "preview")
        if record.preview_mode == "generated" and not service.settings.generated_previews_enabled:
            raise AppError("token_not_found", "The media token is not available.")
        lease = limiter.acquire_download(_request_client_identity(request, trusted_proxy_cidrs))
        if record.preview_mode == "generated":
            return await _generate_preview(
                record,
                media_client,
                thumbnail_generator,
                thumbnail_coordinator,
                lease=lease,
                response_timeout=media_response_timeout_seconds,
            )
        return await _stream_media(
            record,
            media_client,
            preview=True,
            lease=lease,
            response_timeout=media_response_timeout_seconds,
        )

    return router


def _request_client_identity(request: Request, trusted_proxy_cidrs: Iterable[str]) -> str:
    """Reuse the pre-parsed client identity established by the ASGI boundary."""
    identity = getattr(request.state, "client_identity", None)
    if isinstance(identity, str):
        return identity
    return client_identity(
        request.client.host if request.client else "unknown",
        request.headers.get("x-forwarded-for"),
        trusted_proxy_cidrs,
    )


async def _stream_media(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    *,
    preview: bool,
    lease: Lease,
    response_timeout: float | None = None,
) -> StreamingResponse:
    """Fetch, prevalidate, and stream one private media record."""
    source_url = record.source_url
    request_headers = record.request_headers
    filename = record.filename
    deadline = monotonic() + response_timeout if response_timeout is not None else None
    try:
        response = await _await_with_deadline(
            media_client.fetch(source_url, headers=request_headers), deadline
        )
    except BaseException:
        await lease.release()
        raise
    iterator = iter_validated_body(
        response,
        preview=preview,
        expected_media_class=record.media_class,
    )
    try:
        first_chunk = await _await_with_deadline(anext(iterator), deadline)
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

    cleaned = False

    async def cleanup() -> None:
        """Close the upstream response and release its lease exactly once."""
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        try:
            await response.close()
        finally:
            await lease.release()

    async def body() -> AsyncIterator[bytes]:
        """Yield the prevalidated first chunk and remaining upstream bytes."""
        try:
            yield first_chunk
            while True:
                try:
                    chunk = await _await_with_deadline(anext(iterator), deadline)
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            await cleanup()

    try:
        headers = build_forward_response_headers(response, filename=filename, preview=preview)
    except BaseException:
        await cleanup()
        raise
    return DeadlineStreamingResponse(
        body(),
        headers=headers,
        media_type=response.headers.get("content-type"),
        deadline=deadline,
        cleanup=cleanup,
    )


async def _await_with_deadline[T](awaitable: Awaitable[T], deadline: float | None) -> T:
    """Await one operation until an optional complete-response deadline."""
    if deadline is None:
        return await awaitable
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise AppError("upstream_media_invalid", "The media response exceeded its deadline.")
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except TimeoutError as error:
        raise AppError(
            "upstream_media_invalid", "The media response exceeded its deadline."
        ) from error


async def _generate_preview(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
    *,
    lease: Lease,
    response_timeout: float | None = None,
) -> Response:
    """Generate one buffered JPEG and hold its lease through response completion."""
    deadline = monotonic() + response_timeout if response_timeout is not None else None

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
        data = await _await_with_deadline(
            thumbnail_coordinator.get_or_generate(
                record.token, expires_at=record.expires_at, factory=generate
            ),
            deadline,
        )
    except BaseException:
        await lease.release()
        raise
    try:
        headers = build_preview_headers(record.filename)
        headers["Content-Type"] = "image/jpeg"
        headers["Content-Length"] = str(len(data))
        return DeadlineStreamingResponse(
            [data],
            headers=headers,
            media_type="image/jpeg",
            deadline=deadline,
            cleanup=lease.release,
        )
    except BaseException:
        await lease.release()
        raise


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
