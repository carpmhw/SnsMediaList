"""Tests for the FastAPI application shell."""

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
