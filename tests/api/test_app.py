"""Tests for the FastAPI application shell."""

from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sns_media_list.app import create_app
from sns_media_list.config import Settings


def test_starlette_range_regression_is_patched_and_bounded() -> None:
    """Verify static-file Range handling runs on a patched Starlette release."""
    assert tuple(int(part) for part in version("starlette").split(".")[:3]) >= (0, 49, 1)

    response = TestClient(create_app()).get(
        "/styles.css",
        headers={"Range": "bytes=" + ",".join(f"{index}-{index}" for index in range(256))},
    )

    assert response.status_code in {200, 206, 416}


def test_health_endpoint_returns_healthy_status() -> None:
    """Verify the health endpoint returns a small stable response."""
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_returns_json_error() -> None:
    """Verify API clients receive JSON for an unknown API route."""
    client = TestClient(create_app())

    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_oversized_declared_extraction_body_is_rejected_before_parsing() -> None:
    """Verify a declared oversized extraction body never reaches Pydantic parsing."""
    settings = Settings(extraction_body_limit_bytes=32)
    client = TestClient(create_app(settings=settings))

    response = client.post(
        "/api/extractions",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "33",
        },
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response.headers["cache-control"] == "no-store"


def test_unsupported_extraction_encoding_is_rejected_before_parsing() -> None:
    """Verify compressed extraction bodies are rejected before framework parsing."""
    client = TestClient(create_app())

    response = client.post(
        "/api/extractions",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_malformed_extraction_json_uses_stable_error_envelope() -> None:
    """Verify parser details are not exposed for bounded malformed JSON bodies."""
    client = TestClient(create_app())

    response = client.post(
        "/api/extractions",
        content=b'{"url":',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "detail" not in response.json()
    assert response.headers["x-sns-error-code"] == "invalid_request"


def test_extraction_rate_limit_rejects_a_later_body_before_parsing() -> None:
    """Verify rate exhaustion wins over a later malformed or oversized request."""
    settings = Settings(rate_limit_extraction_attempts=1, extraction_body_limit_bytes=32)
    client = TestClient(create_app(settings=settings))

    first = client.post(
        "/api/extractions",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    second = client.post(
        "/api/extractions",
        content=b"x" * 100,
        headers={"Content-Type": "application/json"},
    )

    assert first.status_code == 422
    assert second.status_code == 429
    assert second.json()["code"] == "local_rate_limited"


def test_media_rate_limit_rejects_before_random_token_lookup() -> None:
    """Verify media attempt limits run before token lookup and upstream access."""
    settings = Settings(rate_limit_media_attempts=1)
    client = TestClient(create_app(settings=settings))

    first = client.get("/api/media/not-a-token/download")
    second = client.get("/api/media/not-a-token/download")

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["code"] == "local_rate_limited"


def test_health_and_static_requests_do_not_consume_expensive_attempt_limits() -> None:
    """Verify liveness and static assets remain available under tight API limits."""
    settings = Settings(rate_limit_extraction_attempts=1, rate_limit_media_attempts=1)
    client = TestClient(create_app(settings=settings))

    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/styles.css").status_code == 200


@pytest.mark.asyncio
async def test_chunked_extraction_body_is_bounded_before_framework_parsing() -> None:
    """Verify an unknown-length body stops after crossing the bounded request limit."""
    app = create_app(settings=Settings(extraction_body_limit_bytes=32))
    status, headers, body = await _call_asgi(
        app,
        [b'{"url":"', b"x" * 40],
        [(b"content-type", b"application/json")],
    )

    assert status == 413
    assert dict(headers)[b"cache-control"] == b"no-store"
    assert b'"code":"request_too_large"' in body


async def _call_asgi(
    app: Callable[..., Awaitable[None]],
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Call an ASGI app with explicit body chunks and collect its HTTP response."""
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    response_start: dict[str, Any] = {}
    response_body: list[bytes] = []

    async def receive() -> dict[str, Any]:
        """Return the next explicit request body message to the ASGI app."""
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        """Collect one ASGI response message."""
        if message["type"] == "http.response.start":
            response_start.update(message)
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/extractions",
            "raw_path": b"/api/extractions",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.10", 1234),
            "server": ("testserver", 80),
            "root_path": "",
            "state": {},
        },
        receive,
        send,
    )

    return response_start["status"], response_start["headers"], b"".join(response_body)


def test_application_lifecycle_starts_and_closes_extraction_proxy(monkeypatch) -> None:
    """Verify the application owns the extractor proxy for its full lifespan."""
    calls: list[tuple[str, int]] = []

    class FakeServer:
        """Represent the minimal server lifecycle used by the application."""

        def close(self) -> None:
            """Record that the proxy server was closed."""
            calls.append(("close", 0))

        async def wait_closed(self) -> None:
            """Record that proxy shutdown completed."""
            calls.append(("wait_closed", 0))

    async def fake_serve(self, host: str, port: int) -> FakeServer:
        """Capture the configured loopback proxy bind address."""
        calls.append((host, port))
        return FakeServer()

    monkeypatch.setattr("sns_media_list.network.connect_proxy.ConnectProxy.serve", fake_serve)
    application = create_app(settings=Settings(extraction_proxy_port=8765))

    with TestClient(application):
        assert application.state.extraction_proxy_server is not None

    assert calls == [("127.0.0.1", 8765), ("close", 0), ("wait_closed", 0)]


def test_valid_cookie_mount_keeps_health_local_and_invalid_mount_fails(tmp_path) -> None:
    """Verify valid secret mounts start while invalid configured mounts fail safely."""
    cookie_file = tmp_path / "instagram.cookies.txt"
    cookie_file.write_text("session-cookie-value", encoding="utf-8")

    settings = Settings(instagram_cookie_file=str(cookie_file))
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    invalid_paths = (tmp_path / "missing.cookies.txt", tmp_path / "empty.cookies.txt", tmp_path)
    for invalid_path in invalid_paths:
        if invalid_path.name == "empty.cookies.txt":
            invalid_path.touch()
        with pytest.raises(ValueError):
            Settings(instagram_cookie_file=str(invalid_path))
