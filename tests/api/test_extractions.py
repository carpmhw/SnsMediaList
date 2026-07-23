"""Tests for extraction API composition and stable error responses."""

import json
import logging
from typing import Any

from fastapi.testclient import TestClient

from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.security.tokens import TokenStore
from sns_media_list.services.extraction_service import ExtractionService


class FakeExtractor:
    """Return deterministic gallery metadata without platform requests."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        """Store records and call count for URL-validation assertions."""
        self.records = records
        self.calls = 0

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return configured records and count invocations."""
        self.calls += 1
        return self.records


class AuthenticationFailureExtractor:
    """Raise the stable platform authentication error without producing records."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return the bounded authentication failure used by the API contract test."""
        raise AppError(
            "platform_authentication_failed",
            "The platform session is unavailable. Contact the service operator.",
        )


def record(
    *, num: int = 1, url: str | None = "https://pbs.twimg.com/media/1.jpg?name=orig"
) -> dict[str, Any]:
    """Build one normalized fake gallery record."""
    return {
        "platform": "x",
        "post_url": "https://x.com/creator/status/1",
        "post_id": "1",
        "author": "creator",
        "description": "description",
        "num": num,
        "type": "image",
        "url": url,
        "preview_url": "https://pbs.twimg.com/media/1.jpg?name=small" if url else None,
        "extension": "jpg",
        "width": 1200,
        "height": 800,
        "progressive": url is not None,
    }


def make_client(
    records: list[dict[str, Any]], *, capacity: int = 20
) -> tuple[TestClient, FakeExtractor]:
    """Build an API client with fake extraction and in-memory tokens."""
    extractor = FakeExtractor(records)
    store = TokenStore(capacity=capacity, ttl_seconds=600)
    service = ExtractionService(Settings(), extractor=extractor, token_store=store)
    return TestClient(create_app(extraction_service=service)), extractor


def test_extraction_returns_normalized_media_without_source_url() -> None:
    """Verify successful extraction returns public tokens and ordered metadata."""
    client, _extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform"] == "x"
    assert payload["media"][0]["filename"] == "x-1-1.jpg"
    assert payload["media"][0]["token"]
    assert "source_url" not in json.dumps(payload)


def test_extraction_issues_generated_preview_when_metadata_has_no_poster() -> None:
    """Verify downloadable media always receives an opaque preview URL."""
    media_record = record()
    media_record.update(
        {
            "type": "video",
            "url": "https://video.twimg.com/1.mp4",
            "preview_url": None,
            "extension": "mp4",
        }
    )
    client, _extractor = make_client([media_record])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    media = response.json()["media"][0]
    assert media["preview_url"].startswith("/api/media/")
    assert media["preview_url"].endswith("/preview")


def test_extraction_omits_private_extractor_fields_from_public_response() -> None:
    """Verify credentials, cookies, headers, raw output, and stack traces cannot leak."""
    private_record = record()
    private_record.update(
        {
            "cookies": {"session": "cookie-secret"},
            "request_headers": {"Authorization": "Bearer secret"},
            "raw": "extractor-output",
            "exception": "Traceback (most recent call last)",
        }
    )
    client, _extractor = make_client([private_record])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    response_text = json.dumps(response.json())
    for secret in ("cookie-secret", "Bearer secret", "extractor-output", "Traceback"):
        assert secret not in response_text
    for field in ("cookies", "request_headers", "raw", "exception"):
        assert field not in response.json()


def test_cookie_material_stays_out_of_tokens_responses_and_logs(caplog) -> None:
    """Verify cookie paths and values never cross the application-owned boundary."""
    secret_value = "session-cookie-value"
    secret_path = "/run/secrets/instagram-cookies.txt"
    private_record = record()
    private_record.update(
        {
            "cookies": {"sessionid": secret_value},
            "cookie_file": secret_path,
            "description": "safe description",
        }
    )
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(), extractor=FakeExtractor([private_record]), token_store=token_store
    )
    client = TestClient(create_app(extraction_service=service))
    caplog.set_level(logging.INFO, logger="sns_media_list")

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    assert secret_value not in response.text
    assert secret_path not in response.text
    assert secret_value not in " ".join(record.getMessage() for record in caplog.records)
    assert secret_path not in " ".join(record.getMessage() for record in caplog.records)
    assert all(secret_value not in str(record) for record in token_store._records.values())
    assert all(secret_path not in str(record) for record in token_store._records.values())


def test_health_endpoint_reveals_no_cookie_configuration(tmp_path) -> None:
    """Verify health checks stay local and do not disclose configured secret paths."""
    cookie_file = tmp_path / "x.cookies.txt"
    cookie_file.write_text("session-cookie-value", encoding="utf-8")
    client = TestClient(create_app(settings=Settings(x_cookie_file=str(cookie_file))))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert str(cookie_file) not in response.text
    assert "session-cookie-value" not in response.text


def test_successful_extraction_log_contains_only_safe_event_fields(caplog) -> None:
    """Verify runtime extraction logs omit source URLs, tokens, and descriptions."""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    client, _extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    events = [record.__dict__.get("event") for record in caplog.records]
    assert events
    event = events[-1]
    assert event["outcome"] == "success"
    assert event["item_count"] == 1
    assert "source_url" not in event
    assert "description" not in event
    assert "token" not in event


def test_unsupported_url_does_not_invoke_extractor() -> None:
    """Verify URL validation happens before subprocess extraction."""
    client, extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator"})

    assert response.status_code == 400
    assert response.json()["code"] == "unsupported_url"
    assert extractor.calls == 0


def test_no_media_returns_422() -> None:
    """Verify all-unavailable media uses the stable no-media error."""
    client, _extractor = make_client([record(url=None)])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 422
    assert response.json()["code"] == "no_media"


def test_media_limit_returns_422_without_tokens() -> None:
    """Verify oversized extractor output is rejected before token issuance."""
    records = [
        record(num=index, url=f"https://pbs.twimg.com/media/{index}.jpg") for index in range(1, 22)
    ]
    client, _extractor = make_client(records)

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 422
    assert response.json()["code"] == "extraction_limit_exceeded"


def test_token_capacity_returns_503_atomically() -> None:
    """Verify download and preview token capacity failure is stable."""
    client, _extractor = make_client([record()], capacity=1)

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 503
    assert response.json()["code"] == "capacity_exceeded"


def test_platform_authentication_failure_is_safe_and_issues_no_tokens() -> None:
    """Verify platform session errors are bounded and do not reserve media tokens."""
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(), extractor=AuthenticationFailureExtractor(), token_store=token_store
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 503
    assert response.json()["code"] == "platform_authentication_failed"
    assert response.json()["message"] == (
        "The platform session is unavailable. Contact the service operator."
    )
    assert token_store.size == 0


def test_active_client_limit_returns_retry_after() -> None:
    """Verify API rate limiting is immediate and includes Retry-After."""
    client, _extractor = make_client([record()])
    _lease = client.app.state.limiter.acquire_extraction("testclient")

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "local_rate_limited"
