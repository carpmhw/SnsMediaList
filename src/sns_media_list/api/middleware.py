"""Pure ASGI request boundaries for bounded and privacy-aware API access."""

from collections.abc import Iterable
from json import JSONDecodeError
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import AppError
from ..models import ErrorResponse
from .limits import AttemptKind, AttemptLimiter, client_identity


class SecurityBoundaryMiddleware:
    """Apply request identity, rate limits, body limits, and API headers early."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        body_limit_bytes: int,
        attempt_limiter: AttemptLimiter,
        trusted_proxy_cidrs: Iterable[str],
    ) -> None:
        """Initialize the pure ASGI security boundary."""
        self.app = app
        self.body_limit_bytes = body_limit_bytes
        self.attempt_limiter = attempt_limiter
        self.trusted_proxy_cidrs = tuple(trusted_proxy_cidrs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI scope while preserving non-HTTP application traffic."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = uuid4().hex
        state["request_id"] = request_id
        identity = client_identity(
            _peer_ip(scope),
            _header(scope, b"x-forwarded-for"),
            self.trusted_proxy_cidrs,
        )
        state["client_identity"] = identity

        try:
            route_kind = _route_kind(scope)
            if route_kind is not None:
                self.attempt_limiter.acquire(route_kind, identity)
            if scope["method"] == "POST" and scope["path"] == "/api/extractions":
                body = await self._read_extraction_body(scope, receive)
                receive = _replay_body(body)
        except AppError as error:
            await self._send_error(scope, receive, send, error, request_id)
            return

        async def send_with_security_headers(message: Message) -> None:
            """Attach correlation and cache protections to downstream responses."""
            if message["type"] == "http.response.start":
                message = dict(message)
                response_headers = list(message.get("headers", []))
                _add_header(response_headers, b"x-request-id", request_id.encode("ascii"))
                if scope["path"].startswith("/api/"):
                    _add_header(response_headers, b"cache-control", b"no-store")
                    _add_header(response_headers, b"referrer-policy", b"no-referrer")
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)

    async def _read_extraction_body(self, scope: Scope, receive: Receive) -> bytes:
        """Validate and buffer one bounded unencoded JSON extraction body."""
        content_type = _header(scope, b"content-type")
        if (
            content_type is None
            or content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise AppError(
                "unsupported_media_type",
                "The extraction request must use an unencoded JSON body.",
            )
        content_encoding = _header(scope, b"content-encoding")
        if content_encoding and content_encoding.strip().lower() != "identity":
            raise AppError(
                "unsupported_media_type",
                "The extraction request must use an unencoded JSON body.",
            )
        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.body_limit_bytes:
                    raise AppError(
                        "request_too_large",
                        "The extraction request body is too large.",
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b""
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.body_limit_bytes:
                raise AppError("request_too_large", "The extraction request body is too large.")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        error: AppError,
        request_id: str,
    ) -> None:
        """Send one safe application error without invoking downstream parsing."""
        try:
            payload = ErrorResponse(
                code=error.code,
                message=error.message,
                request_id=request_id,
            ).model_dump()
        except (TypeError, ValueError, JSONDecodeError):
            payload = {
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
            }
        response = JSONResponse(status_code=error.status_code or 500, content=payload)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        response.headers["X-SNS-Error-Code"] = error.code
        await response(scope, receive, send)


def _route_kind(scope: Scope) -> AttemptKind | None:
    """Return the rate-limit class for one expensive HTTP route."""
    path = scope["path"]
    method = scope["method"]
    if method == "POST" and path == "/api/extractions":
        return "extraction"
    if path.startswith("/api/media/") and method == "GET":
        return "media"
    return None


def _peer_ip(scope: Scope) -> str:
    """Return the socket peer address from an HTTP ASGI scope."""
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return str(client[0])
    return "unknown"


def _header(scope: Scope, name: bytes) -> str | None:
    """Return one decoded request header from an ASGI scope."""
    for key, value in scope.get("headers", []):
        if isinstance(key, bytes) and key.lower() == name and isinstance(value, bytes):
            return value.decode("latin-1")
    return None


def _add_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    """Append a response header only when a downstream response did not set it."""
    if not any(existing_name.lower() == name for existing_name, _existing_value in headers):
        headers.append((name, value))


def _replay_body(body: bytes) -> Receive:
    """Create a one-shot ASGI receive callable for a validated body."""
    delivered = False

    async def receive() -> Message:
        """Return the buffered body once and an empty terminal message thereafter."""
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive
