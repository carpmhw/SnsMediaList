"""Tests for the FastAPI application shell."""

import pytest
from fastapi.testclient import TestClient

from sns_media_list.app import create_app
from sns_media_list.config import Settings


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
