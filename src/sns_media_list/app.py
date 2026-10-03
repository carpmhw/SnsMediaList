"""FastAPI application factory and process-level middleware."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from .api.limits import AttemptLimiter, RequestLimiter
from .api.middleware import SecurityBoundaryMiddleware
from .api.routes import build_router
from .config import Settings, get_settings
from .errors import AppError
from .extractor.gallery_dl import GalleryDlRunner
from .logging_config import configure_logging, log_mcp_event
from .mcp.middleware import McpBoundaryMiddleware, McpRequestRegistry
from .mcp.server import McpRuntimeFactory, create_mcp_runtime
from .models import ErrorResponse
from .network.connect_proxy import ConnectProxy
from .network.dns import DestinationPolicy, resolve_system
from .network.media_client import MediaClient, MediaDestinationPolicy
from .security.tokens import TokenStore
from .services.extraction_coordinator import ExtractionCoordinator
from .services.extraction_service import ExtractionService
from .services.thumbnail import ThumbnailGenerator
from .services.thumbnail_cache import ThumbnailCache, ThumbnailCoordinator

_EXTRACTION_HOSTS = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "i.instagram.com",
        "graph.instagram.com",
        "static.cdninstagram.com",  # Story GraphQL doc_id 所需的靜態 JavaScript。
        "x.com",
        "www.x.com",
        "api.x.com",
        "twitter.com",
        "www.twitter.com",
        "api.twitter.com",
        "abs.twimg.com",
        "pbs.twimg.com",
        "video.twimg.com",
    }
)


def create_app(
    *,
    settings: Settings | None = None,
    extraction_service: ExtractionService | None = None,
    media_client: MediaClient | None = None,
    extraction_proxy: ConnectProxy | None = None,
    thumbnail_generator: ThumbnailGenerator | None = None,
    thumbnail_coordinator: ThumbnailCoordinator | None = None,
    mcp_factory: McpRuntimeFactory | None = None,
) -> FastAPI:
    """建立 app-local 核心相依與各 transport 共用的擷取 budget。"""
    configure_logging()
    settings = settings or get_settings()
    if extraction_proxy is None:
        extraction_proxy = ConnectProxy(
            DestinationPolicy(allowed_hosts=_EXTRACTION_HOSTS, resolver=resolve_system),
            operation_timeout_seconds=settings.extraction_timeout_seconds,
        )
    if extraction_service is None:
        extraction_service = ExtractionService(
            settings,
            extractor=GalleryDlRunner(settings),
            token_store=TokenStore(
                capacity=settings.token_capacity,
                ttl_seconds=settings.token_ttl_seconds,
            ),
        )
    if media_client is None:
        media_policy = MediaDestinationPolicy(
            allowed_exact_hosts=frozenset({"pbs.twimg.com", "video.twimg.com"}),
            allowed_suffixes=frozenset({"cdninstagram.com", "fbcdn.net"}),
            resolver=resolve_system,
        )
        media_client = MediaClient(
            media_policy,
            max_redirects=settings.max_redirects,
            connect_timeout=settings.connect_timeout_seconds,
            max_bytes=settings.max_download_bytes,
            read_timeout=settings.read_timeout_seconds,
        )
    limiter = RequestLimiter(
        max_extractions=settings.max_extractions,
        max_downloads=settings.max_downloads,
        max_downloads_per_client=settings.max_downloads_per_client,
    )
    extraction_coordinator = ExtractionCoordinator(extraction_service, limiter=limiter)
    attempt_limiter = AttemptLimiter(
        extraction_limit=settings.rate_limit_extraction_attempts,
        media_limit=settings.rate_limit_media_attempts,
        window_seconds=settings.rate_limit_window_seconds,
        max_identities=settings.rate_limit_identity_capacity,
        reserve_mcp_cancellation=settings.mcp_enabled,
    )
    mcp_runtime = (
        (mcp_factory or create_mcp_runtime)(extraction_coordinator, settings)
        if settings.mcp_enabled
        else None
    )
    thumbnail_generator = thumbnail_generator or ThumbnailGenerator(
        input_bytes=settings.thumbnail_input_bytes,
        output_bytes=settings.thumbnail_output_bytes,
        timeout_seconds=settings.thumbnail_timeout_seconds,
        max_edge=settings.thumbnail_max_edge,
    )
    thumbnail_cache = ThumbnailCache(
        max_bytes=settings.thumbnail_cache_bytes,
        max_negative_entries=settings.token_capacity,
    )
    thumbnail_coordinator = thumbnail_coordinator or ThumbnailCoordinator(
        thumbnail_cache,
        max_concurrency=settings.thumbnail_concurrency,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Parent 擁有 proxy／MCP 啟停，任何部分失敗仍清理已取得資源。"""
        server = await extraction_proxy.serve(
            settings.extraction_proxy_host,
            settings.extraction_proxy_port,
        )
        application.state.extraction_proxy_server = server
        try:
            if mcp_runtime is None:
                yield
            else:
                started = perf_counter()
                entered = False
                request_id = uuid4().hex
                try:
                    async with asyncio.timeout(None) as shutdown_deadline:
                        async with mcp_runtime.run():
                            entered = True
                            log_mcp_event(
                                "mcp_server_started",
                                request_id=request_id,
                                platform=None,
                                outcome="success",
                                duration_ms=(perf_counter() - started) * 1000,
                            )
                            try:
                                yield
                            finally:
                                shutdown_deadline.reschedule(asyncio.get_running_loop().time() + 5)
                except BaseException:
                    if not entered:
                        log_mcp_event(
                            "mcp_server_failed",
                            request_id=request_id,
                            platform=None,
                            outcome="failed",
                            reason_code="startup_failed",
                            duration_ms=(perf_counter() - started) * 1000,
                        )
                    raise
        finally:
            server.close()
            with anyio.CancelScope(shield=True):
                async with asyncio.timeout(5):
                    try:
                        await server.wait_closed()
                    finally:
                        await extraction_proxy.close_clients()

    application = FastAPI(
        title="SNS Media List",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.limiter = limiter
    application.state.attempt_limiter = attempt_limiter
    application.state.mcp_runtime = mcp_runtime
    application.state.thumbnail_cache = thumbnail_cache
    application.state.thumbnail_coordinator = thumbnail_coordinator

    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
        """Convert an application error into the public JSON error envelope."""
        payload = ErrorResponse(
            code=error.code,
            message=error.message,
            request_id=getattr(request.state, "request_id", "unknown"),
        )
        response = JSONResponse(status_code=error.status_code or 500, content=payload.model_dump())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        response.headers["X-SNS-Error-Code"] = error.code
        return response

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        """Convert framework validation failures into a stable safe error envelope."""
        return await handle_app_error(
            request,
            AppError("invalid_request", "The request is invalid."),
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Return the liveness status without contacting external services."""
        return {"status": "ok"}

    application.include_router(
        build_router(
            extraction_service,
            media_client,
            limiter=limiter,
            extraction_coordinator=extraction_coordinator,
            trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
            media_response_timeout_seconds=settings.media_response_timeout_seconds,
            thumbnail_generator=thumbnail_generator,
            thumbnail_coordinator=thumbnail_coordinator,
        )
    )

    application.add_middleware(
        SecurityBoundaryMiddleware,
        body_limit_bytes=settings.extraction_body_limit_bytes,
        attempt_limiter=attempt_limiter,
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
    )

    if mcp_runtime is not None:
        application.router.routes.append(
            Route(
                "/mcp",
                endpoint=McpBoundaryMiddleware(
                    mcp_runtime.http_app,
                    body_limit_bytes=settings.mcp_max_request_body_bytes,
                    attempt_limiter=attempt_limiter,
                    request_registry=mcp_runtime.request_registry or McpRequestRegistry(),
                ),
            )
        )
    else:
        application.router.routes.append(
            Route(
                "/mcp",
                endpoint=JSONResponse(status_code=404, content={"detail": "Not Found"}),
            )
        )
    # 保留 MCP namespace 的 404 語意，避免 static catch-all 將錯誤 POST 路徑變成 405。
    application.router.routes.append(
        Route(
            "/mcp/{path:path}",
            endpoint=JSONResponse(status_code=404, content={"detail": "Not Found"}),
        )
    )

    static_dir = Path(__file__).parent / "static"
    application.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return application
