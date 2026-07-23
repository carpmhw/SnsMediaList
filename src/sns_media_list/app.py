"""FastAPI application factory and process-level middleware."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .api.limits import RequestLimiter
from .api.routes import build_router
from .config import Settings, get_settings
from .errors import AppError
from .extractor.gallery_dl import GalleryDlRunner
from .logging_config import configure_logging
from .models import ErrorResponse
from .network.connect_proxy import ConnectProxy
from .network.dns import DestinationPolicy, resolve_system
from .network.media_client import MediaClient, MediaDestinationPolicy
from .security.tokens import TokenStore
from .services.extraction_service import ExtractionService
from .services.thumbnail import ThumbnailGenerator
from .services.thumbnail_cache import ThumbnailCache, ThumbnailCoordinator

_EXTRACTION_HOSTS = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "i.instagram.com",
        "graph.instagram.com",
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
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    configure_logging()
    settings = settings or get_settings()
    if extraction_proxy is None:
        extraction_proxy = ConnectProxy(
            DestinationPolicy(allowed_hosts=_EXTRACTION_HOSTS, resolver=resolve_system)
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
            total_timeout=settings.download_timeout_seconds,
        )
    limiter = RequestLimiter(
        max_extractions=settings.max_extractions,
        max_downloads=settings.max_downloads,
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
        """Start the extractor egress guard and close it during shutdown."""
        server = await extraction_proxy.serve(
            settings.extraction_proxy_host,
            settings.extraction_proxy_port,
        )
        application.state.extraction_proxy_server = server
        try:
            yield
        finally:
            server.close()
            await server.wait_closed()
            await extraction_proxy.close_clients()

    application = FastAPI(
        title="SNS Media List",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.limiter = limiter
    application.state.thumbnail_cache = thumbnail_cache
    application.state.thumbnail_coordinator = thumbnail_coordinator

    @application.middleware("http")
    async def attach_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a request ID to request state and the response headers."""
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
        """Convert an application error into the public JSON error envelope."""
        payload = ErrorResponse(
            code=error.code,
            message=error.message,
            request_id=getattr(request.state, "request_id", "unknown"),
        )
        response = JSONResponse(status_code=error.status_code or 500, content=payload.model_dump())
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Return the liveness status without contacting external services."""
        return {"status": "ok"}

    application.include_router(
        build_router(
            extraction_service,
            media_client,
            limiter=limiter,
            trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
            thumbnail_generator=thumbnail_generator,
            thumbnail_coordinator=thumbnail_coordinator,
        )
    )

    static_dir = Path(__file__).parent / "static"
    application.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return application
