"""Tests for extraction API composition and stable error responses."""

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import pytest
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


class StoryUnavailableExtractor:
    """Raise the stable generic Story availability error without producing records."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Raise the bounded Story error used by the API contract test."""
        raise AppError("story_unavailable", "This Story is unavailable.")


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


@pytest.mark.parametrize(
    ("story_id", "media_type", "extension"),
    [
        pytest.param("1111111111111111111", "image", "jpg", id="image"),
        pytest.param("2222222222222222222", "video", "mp4", id="video"),
    ],
)
def test_story_success_preserves_response_shape_and_opaque_media_urls(
    story_id: str,
    media_type: str,
    extension: str,
) -> None:
    """Verify image and video Stories use the existing application-owned API contract."""
    story_url = f"https://www.instagram.com/stories/example.user/{story_id}/"
    source_url = f"https://scontent.cdninstagram.com/story-{media_type}.{extension}?private=1"
    preview_source_url = f"https://scontent.cdninstagram.com/story-{media_type}-preview.jpg"
    story_record = record()
    story_record.update(
        {
            "platform": "instagram",
            "post_url": story_url,
            "post_id": story_id,
            "type": media_type,
            "url": source_url,
            "preview_url": preview_source_url,
            "extension": extension,
        }
    )
    client, _extractor = make_client([story_record])

    response = client.post("/api/extractions", json={"url": story_url})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "platform",
        "post_url",
        "author",
        "description",
        "unavailable_media_count",
        "media",
    }
    assert payload["platform"] == "instagram"
    assert payload["post_url"] == story_url
    assert len(payload["media"]) == 1
    media = payload["media"][0]
    assert set(media) == {
        "token",
        "media_type",
        "filename",
        "width",
        "height",
        "duration",
        "preview_url",
        "download_url",
    }
    assert media["filename"] == f"instagram-{story_id}-1.{extension}"
    assert media["media_type"] == media_type

    preview_url = urlsplit(media["preview_url"])
    download_url = urlsplit(media["download_url"])
    for public_url, purpose in ((preview_url, "preview"), (download_url, "download")):
        assert (public_url.scheme, public_url.netloc, public_url.query, public_url.fragment) == (
            "",
            "",
            "",
            "",
        )
        path_parts = public_url.path.split("/")
        assert path_parts[:3] == ["", "api", "media"]
        assert len(path_parts) == 5
        assert len(path_parts[3]) >= 32
        assert path_parts[4] == purpose
        assert story_id not in public_url.path
    assert download_url.path == f"/api/media/{media['token']}/download"
    assert source_url not in response.text
    assert preview_source_url not in response.text


def test_story_sensitive_metadata_stays_out_of_response_tokens_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify authenticated Story metadata cannot cross application-owned boundaries."""
    story_id = "3333333333333333333"
    story_url = f"https://www.instagram.com/stories/example.user/{story_id}/"
    source_url = "https://scontent.cdninstagram.com/sensitive-story.jpg?source=private"
    preview_source_url = (
        "https://scontent.cdninstagram.com/sensitive-story-preview.jpg?source=private"
    )
    secrets = {
        "audience": "close-friends-audience",
        "close_friends": "close-friends-only",
        "subscription": "subscriber-only",
        "cookie": "session-cookie-secret",
        "cookie_file": "/run/secrets/instagram-cookies.txt",
        "authorization": "Bearer extractor-secret",
        "header": "extractor-header-secret",
        "raw": "raw-story-metadata",
        "exception": "private-story-traceback",
        "token": "extractor-token-secret",
    }
    story_record = record()
    story_record.update(
        {
            "platform": "instagram",
            "post_url": story_url,
            "post_id": story_id,
            "url": source_url,
            "preview_url": preview_source_url,
            "audience": secrets["audience"],
            "close_friends": secrets["close_friends"],
            "subscription": secrets["subscription"],
            "cookies": {"sessionid": secrets["cookie"]},
            "cookie_file": secrets["cookie_file"],
            "request_headers": {
                "Cookie": f"sessionid={secrets['cookie']}",
                "Authorization": secrets["authorization"],
            },
            "headers": {"X-Extractor": secrets["header"]},
            "raw": {"diagnostic": secrets["raw"]},
            "exception": secrets["exception"],
            "token": secrets["token"],
        }
    )
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(), extractor=FakeExtractor([story_record]), token_store=token_store
    )
    client = TestClient(create_app(extraction_service=service))
    caplog.set_level(logging.INFO, logger="sns_media_list")

    response = client.post("/api/extractions", json={"url": story_url})

    assert response.status_code == 200
    media = response.json()["media"][0]
    download_token = media["token"]
    preview_token = urlsplit(media["preview_url"]).path.split("/")[-2]
    assert urlsplit(media["download_url"]).path.split("/")[-2] == download_token
    download_record = token_store.get(download_token, "download")
    preview_record = token_store.get(preview_token, "preview")
    assert download_record.token == download_token
    assert download_record.purpose == "download"
    assert download_record.source_url == source_url
    assert preview_record.token == preview_token
    assert preview_record.purpose == "preview"
    assert preview_record.source_url == preview_source_url

    expected_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
    }
    for token_record in (download_record, preview_record):
        assert token_record.media_class == "image"
        assert token_record.filename == f"instagram-{story_id}-1.jpg"
        assert token_record.platform == "instagram"
        assert token_record.expires_at > 0
        assert token_record.content_type is None
        assert token_record.preview_mode == "proxy"
        assert token_record.token != secrets["token"]
        assert token_record.request_headers == expected_headers
        for sensitive_field in (
            "audience",
            "close_friends",
            "subscription",
            "cookies",
            "cookie_file",
            "headers",
            "raw",
            "exception",
        ):
            assert not hasattr(token_record, sensitive_field)
        assert "Cookie" not in token_record.request_headers
        assert "Authorization" not in token_record.request_headers

    for private_value in (*secrets.values(), source_url, preview_source_url):
        assert private_value not in response.text

    application_records = [
        log_record
        for log_record in caplog.records
        if log_record.name == "sns_media_list" or log_record.name.startswith("sns_media_list.")
    ]
    assert application_records
    events = []
    log_private_values = (
        *secrets.values(),
        source_url,
        preview_source_url,
        download_token,
        preview_token,
    )
    for log_record in application_records:
        record_text = json.dumps(log_record.__dict__, sort_keys=True, default=str)
        for private_value in log_private_values:
            assert private_value not in record_text
        event = log_record.__dict__.get("event")
        if event is not None:
            assert set(event) == {
                "request_id",
                "platform",
                "outcome",
                "duration_ms",
                "item_count",
            }
            assert event["platform"] == "instagram"
            events.append(event)
    assert events


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


@pytest.mark.parametrize(
    ("url", "expected_message"),
    [
        pytest.param(
            "https://www.instagram.com/stories/example.user/",
            "Only supported single media target URLs are accepted.",
            id="account-wide-story",
        ),
        pytest.param(
            "https://www.instagram.com/stories/highlights/1234567890/",
            "Only supported single media target URLs are accepted.",
            id="highlight",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/1234567890",
            "Only supported single media target URLs are accepted.",
            id="malformed-story-target",
        ),
        pytest.param(
            "https://www.instagram.com/stories/example.user/not-numeric/",
            "Only supported single media target URLs are accepted.",
            id="non-numeric-story-target",
        ),
        pytest.param(
            "http://www.instagram.com/stories/example.user/1234567890/",
            "Only supported HTTPS media target URLs are accepted.",
            id="non-https-exact-story",
        ),
    ],
)
def test_rejected_story_variants_do_not_invoke_extractor(url: str, expected_message: str) -> None:
    """Verify rejected Story variants stop before subprocess extraction."""
    client, extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": url})

    assert response.status_code == 400
    assert response.json()["code"] == "unsupported_url"
    assert response.json()["message"] == expected_message
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


def test_story_unavailable_is_safe_and_issues_no_tokens() -> None:
    """Verify exact Story availability errors are generic and reserve no media tokens."""
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(), extractor=StoryUnavailableExtractor(), token_store=token_store
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert token_store.size == 0
    assert response.json()["code"] == "story_unavailable"
    assert response.json()["message"] == "This Story is unavailable."
    assert response.status_code == 404


def test_active_client_limit_returns_retry_after() -> None:
    """Verify API rate limiting is immediate and includes Retry-After."""
    client, _extractor = make_client([record()])
    _lease = client.app.state.limiter.acquire_extraction("testclient")

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "local_rate_limited"
