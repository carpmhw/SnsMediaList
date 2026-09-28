"""Tests for extraction API composition and stable error responses."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from sns_media_list.api import routes
from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError, ExtractorDiagnostics
from sns_media_list.extractor.gallery_dl import GalleryDlRunner
from sns_media_list.logging_config import SafeEventFormatter
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


class SyntheticGalleryProcess:
    """提供合成 gallery-dl stdout 的 subprocess 替身。"""

    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        """保存合成 stdout 與程序結束狀態。"""
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        """回傳測試指定的 DataJob 輸出與空 stderr。"""
        return self.stdout, b""

    def terminate(self) -> None:
        """記錄測試期間的 graceful termination。"""
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        """記錄測試期間的強制 termination。"""
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        """回傳合成程序的結束狀態。"""
        return self.returncode


def install_synthetic_gallery_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> list[tuple[Any, ...]]:
    """將合成 DataJob stdout 接入真實 GalleryDlRunner 並回傳 command。"""
    calls: list[tuple[Any, ...]] = []
    process = SyntheticGalleryProcess(output)

    async def fake_create(*args: Any, **_kwargs: Any) -> SyntheticGalleryProcess:
        """擷取 command arguments 並回傳合成 extractor process。"""
        calls.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return calls


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


class FixedErrorExtractor:
    """以指定的穩定 application error 模擬擷取失敗。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_stage: str | None = None,
        extractor_diagnostics: ExtractorDiagnostics | None = None,
    ) -> None:
        """保存 API 契約測試要驗證的安全錯誤內容。"""
        self.code = code
        self.message = message
        self.failure_stage = failure_stage
        self.extractor_diagnostics = extractor_diagnostics

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """拋出指定的 bounded error，不產生 extractor media records。"""
        raise AppError(
            self.code,
            self.message,
            failure_stage=self.failure_stage,
            extractor_diagnostics=self.extractor_diagnostics,
        )


class BlockingExtractor:
    """提供可取消的擷取工作以驗證 terminal event 邊界。"""

    def __init__(self) -> None:
        """建立等待事件供 API 測試同步工作啟動與取消。"""
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """等待測試取消請求，避免完成擷取或輸出 success event。"""
        self.started.set()
        await self.release.wait()
        return [record()]


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
    records: list[dict[str, Any]], *, capacity: int = 20, settings: Settings | None = None
) -> tuple[TestClient, FakeExtractor]:
    """Build an API client with fake extraction and in-memory tokens."""
    extractor = FakeExtractor(records)
    store = TokenStore(capacity=capacity, ttl_seconds=600)
    service = ExtractionService(settings or Settings(), extractor=extractor, token_store=store)
    return TestClient(create_app(extraction_service=service)), extractor


def test_extraction_returns_normalized_media_without_private_values() -> None:
    """驗證成功回應僅保留 application-owned opaque URL，不含私有值。"""
    client, _extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform"] == "x"
    assert payload["media"][0]["filename"] == "x-creator-1-01.jpg"
    media = payload["media"][0]
    assert "token" not in media
    assert media["preview_url"].startswith("/api/media/")
    assert media["download_url"].startswith("/api/media/")
    assert "source_url" not in json.dumps(payload)


def test_extraction_omits_missing_author_from_filename() -> None:
    """作者缺失時 API 應使用穩定的 platform-post-index fallback 檔名。"""
    media_record = record()
    media_record.pop("author")
    client, _extractor = make_client([media_record])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    assert response.json()["media"][0]["filename"] == "x-1-01.jpg"


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
    client, _extractor = make_client(
        [media_record], settings=Settings(generated_previews_enabled=True)
    )

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    media = response.json()["media"][0]
    assert media["preview_url"].startswith("/api/media/")
    assert media["preview_url"].endswith("/preview")


def test_extraction_uses_local_placeholder_when_generated_previews_are_disabled() -> None:
    """Verify missing posters do not reserve or expose generated preview tokens by default."""
    media_record = record()
    media_record.update(
        {
            "type": "video",
            "url": "https://video.twimg.com/1.mp4",
            "preview_url": None,
            "extension": "mp4",
        }
    )
    store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(), extractor=FakeExtractor([media_record]), token_store=store
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    assert response.json()["media"][0]["preview_url"] == "/placeholder.svg"
    assert store.size == 1


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
    assert source_url not in response.text
    assert preview_source_url not in response.text


def test_story_pseudo_url_api_uses_same_record_video_source_and_private_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """驗證實際 runner 將 Story direct video source 私存並只回傳 opaque URL。"""
    story_id = "4444444444444444444"
    story_url = f"https://www.instagram.com/stories/example.user/{story_id}/"
    pseudo_url = "ytdl:api-contract-story-video"
    source_url = "https://cdn.example/story-video.mp4?source=cdn-url-sentinel"
    raw_metadata = "raw-story-metadata-sentinel"
    cookie_sentinel = "synthetic-cookie-sentinel"
    cookie_file = tmp_path / "synthetic-instagram-cookies.txt"
    cookie_file.write_text(cookie_sentinel, encoding="utf-8")
    output = json.dumps(
        [
            [
                3,
                pseudo_url,
                {
                    "platform": "x",
                    "post_url": "https://x.com/untrusted/status/1",
                    "post_id": "untrusted-id",
                    "num": 1,
                    "type": "video",
                    "video_url": source_url,
                    "extension": "mp4",
                    "progressive": True,
                    "raw": {"metadata": raw_metadata},
                    "cookies": {"sessionid": cookie_sentinel},
                    "cookie_file": str(cookie_file),
                },
            ]
        ]
    ).encode()
    calls = install_synthetic_gallery_subprocess(monkeypatch, output)
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    settings = Settings(instagram_cookie_file=str(cookie_file))
    service = ExtractionService(
        settings,
        extractor=GalleryDlRunner(settings),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))
    caplog.set_level(logging.INFO, logger="sns_media_list")

    response = client.post("/api/extractions", json={"url": story_url})

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform"] == "instagram"
    assert payload["post_url"] == story_url
    assert len(payload["media"]) == 1
    media = payload["media"][0]
    assert media["media_type"] == "video"
    assert media["filename"] == f"instagram-{story_id}-1.mp4"
    assert media["preview_url"] == "/placeholder.svg"
    assert media["download_url"].startswith("/api/media/")
    assert source_url not in response.text
    assert pseudo_url not in response.text
    assert raw_metadata not in response.text
    assert cookie_sentinel not in response.text
    assert len(calls) == 1
    assert "extractor.instagram.videos=merged" not in calls[0]
    assert f"extractor.instagram.cookies={cookie_file}" in calls[0]
    assert "extractor.instagram.cookies-update=false" in calls[0]

    download_token = urlsplit(media["download_url"]).path.split("/")[-2]
    private_record = token_store.get(download_token, "download")
    assert private_record.source_url == source_url
    assert private_record.platform == "instagram"
    assert private_record.media_class == "video"
    assert private_record.filename == f"instagram-{story_id}-1.mp4"
    assert private_record.request_headers == {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
    }
    log_text = json.dumps([record.__dict__ for record in caplog.records], default=str)
    for private_value in (source_url, pseudo_url, raw_metadata, cookie_sentinel, str(cookie_file)):
        assert private_value not in log_text


@pytest.mark.parametrize(
    ("include_video_url", "video_url"),
    [
        pytest.param(False, None, id="missing-source"),
        pytest.param(True, "http://cdn.example/unsafe-story.mp4", id="unsafe-source"),
    ],
)
def test_story_missing_or_unsafe_pseudo_source_issues_no_token_or_private_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    include_video_url: bool,
    video_url: str | None,
) -> None:
    """驗證 missing 或 unsafe Story source 在 token reserve 前 fail closed。"""
    story_id = "5555555555555555555"
    story_url = f"https://www.instagram.com/stories/example.user/{story_id}/"
    pseudo_url = "ytdl:api-failure-pseudo-sentinel"
    raw_metadata = "raw-failure-metadata-sentinel"
    cookie_sentinel = "failure-cookie-sentinel"
    cookie_file = tmp_path / "synthetic-instagram-cookies.txt"
    cookie_file.write_text(cookie_sentinel, encoding="utf-8")
    metadata: dict[str, object] = {
        "platform": "instagram",
        "post_url": story_url,
        "post_id": story_id,
        "num": 1,
        "type": "video",
        "extension": "mp4",
        "progressive": True,
        "raw": {"metadata": raw_metadata},
        "cookies": {"sessionid": cookie_sentinel},
    }
    if include_video_url:
        metadata["video_url"] = video_url
    output = json.dumps([[3, pseudo_url, metadata]]).encode()
    install_synthetic_gallery_subprocess(monkeypatch, output)
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    settings = Settings(instagram_cookie_file=str(cookie_file))
    service = ExtractionService(
        settings,
        extractor=GalleryDlRunner(settings),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))
    caplog.set_level(logging.INFO, logger="sns_media_list")

    response = client.post("/api/extractions", json={"url": story_url})

    assert response.status_code == 502
    assert response.json()["code"] == "extraction_failed"
    assert token_store.size == 0
    for private_value in (pseudo_url, raw_metadata, cookie_sentinel, video_url or ""):
        if private_value:
            assert private_value not in response.text
    log_text = json.dumps([record.__dict__ for record in caplog.records], default=str)
    for private_value in (
        pseudo_url,
        raw_metadata,
        cookie_sentinel,
        str(cookie_file),
        video_url or "",
    ):
        if private_value:
            assert private_value not in log_text


def test_story_width_keyerror_keeps_generic_fallback_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """驗證合成 KeyError width 延用一般 extraction_failed 與私有診斷契約。"""
    story_id = "6666666666666666666"
    story_url = f"https://www.instagram.com/stories/example.user/{story_id}/"
    cookie_file = tmp_path / "synthetic-instagram-cookies.txt"
    cookie_file.write_text("width-keyerror-cookie-sentinel", encoding="utf-8")
    output = json.dumps([[-1, {"error": "KeyError", "message": "'width'"}]]).encode()
    install_synthetic_gallery_subprocess(monkeypatch, output)
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    settings = Settings(instagram_cookie_file=str(cookie_file))
    service = ExtractionService(
        settings,
        extractor=GalleryDlRunner(settings),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))
    caplog.set_level(logging.INFO, logger="sns_media_list")

    response = client.post("/api/extractions", json={"url": story_url})

    assert response.status_code == 502
    assert response.json()["code"] == "extraction_failed"
    assert token_store.size == 0
    assert "failure_stage" not in response.text
    assert "extractor_diagnostics" not in response.text
    assert "'width'" not in response.text
    events = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]
    assert len(events) == 1
    assert events[0]["failure_stage"] == "extractor_process_unclassified"
    assert events[0]["extractor_diagnostic_source"] == "datajob_error"
    assert events[0]["extractor_error_type"] == "unknown"
    assert events[0]["extractor_exit_code"] == 0


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
    download_token = urlsplit(media["download_url"]).path.split("/")[-2]
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


@pytest.mark.parametrize(
    ("code", "reason_code"),
    [
        pytest.param("story_auth_required", "story_auth_required", id="story-auth-required"),
        pytest.param(
            "platform_authentication_failed",
            "platform_authentication_failed",
            id="configured-session-failure",
        ),
        pytest.param("extraction_failed", "extraction_failed", id="ambiguous-upstream-error"),
    ],
)
def test_failed_extraction_logs_one_safe_terminal_event(
    caplog,
    code: str,
    reason_code: str,
) -> None:
    """驗證擷取 AppError 只記錄一筆安全失敗 terminal event。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(),
        extractor=FixedErrorExtractor(code, "PRIVATE_RAW_EXTRACTOR_DIAGNOSTIC"),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert response.json()["code"] == code
    assert token_store.size == 0
    events = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]
    assert len(events) == 1
    event = events[0]
    assert event["request_id"] == response.json()["request_id"]
    assert event["platform"] == "instagram"
    assert event["outcome"] == "failed"
    assert event["reason_code"] == reason_code
    assert not client.app.state.limiter._active_extractions
    assert "item_count" not in event
    assert "PRIVATE_RAW_EXTRACTOR_DIAGNOSTIC" not in json.dumps(event)


def test_extractor_diagnostics_are_logged_but_never_exposed_by_api(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """驗證 configured Story 的上游 403 只進唯一安全事件，不進公開契約。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    cookie_file = tmp_path / "synthetic-instagram-cookies.txt"
    cookie_file.write_text("synthetic-cookie", encoding="utf-8")
    diagnostics = ExtractorDiagnostics(
        extractor_diagnostic_source="datajob_error",
        extractor_error_type="http_error",
        extractor_exit_code=0,
        extractor_http_statuses=[403],
    )
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(instagram_cookie_file=str(cookie_file)),
        extractor=FixedErrorExtractor(
            "extraction_failed",
            "The source platform could not be extracted.",
            failure_stage="extractor_process_unclassified",
            extractor_diagnostics=diagnostics,
        ),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "extraction_failed"
    assert token_store.size == 0
    events = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and record.msg == "extraction_failed"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["request_id"] == response.json()["request_id"]
    assert event["failure_stage"] == "extractor_process_unclassified"
    assert event["extractor_diagnostic_source"] == "datajob_error"
    assert event["extractor_error_type"] == "http_error"
    assert event["extractor_exit_code"] == 0
    assert event["extractor_http_statuses"] == [403]

    private_field_names = (
        "failure_stage",
        "extractor_diagnostics",
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    )
    response_payload = json.dumps(response.json())
    openapi_payload = json.dumps(client.get("/openapi.json").json())
    for field_name in private_field_names:
        assert field_name not in response_payload
        assert all(field_name not in name.lower() for name in response.headers)
        assert field_name not in openapi_payload


def test_unsupported_host_extraction_failure_logs_null_platform(caplog) -> None:
    """驗證不支援 host 的 extraction failure 使用 null platform 標籤。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    service = ExtractionService(
        Settings(),
        extractor=FakeExtractor([]),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post("/api/extractions", json={"url": "https://example.invalid/p/1/"})

    assert response.status_code == 400
    event_records = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]
    assert len(event_records) == 1
    assert event_records[0]["platform"] is None
    assert event_records[0]["reason_code"] == "unsupported_url"
    assert "failure_stage" not in event_records[0]


def test_unknown_app_error_code_is_bounded_in_extraction_log(caplog) -> None:
    """驗證未知 AppError code 降為固定日誌分類而不記錄原始診斷。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(),
        extractor=FixedErrorExtractor("private://sessionid=PRIVATE_CODE", "PRIVATE_MESSAGE"),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 500
    events = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]
    assert len(events) == 1
    assert events[0]["reason_code"] == "extraction_failed"
    assert "private://sessionid=PRIVATE_CODE" not in json.dumps(events[0])
    assert "PRIVATE_MESSAGE" not in json.dumps(events[0])


@pytest.mark.parametrize(
    ("failure_stage", "error_code", "expected_status"),
    [
        pytest.param("event_builder", None, 200, id="success-builder-failure"),
        pytest.param("logger", None, 200, id="success-logger-failure"),
        pytest.param("formatter", None, 200, id="success-formatter-failure"),
        pytest.param("output", None, 200, id="success-output-failure"),
        pytest.param("event_builder", "story_auth_required", 403, id="error-builder-failure"),
        pytest.param("logger", "story_auth_required", 403, id="error-logger-failure"),
        pytest.param("formatter", "story_auth_required", 403, id="error-formatter-failure"),
        pytest.param("output", "story_auth_required", 403, id="error-output-failure"),
    ],
)
def test_extraction_logging_failure_preserves_api_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    error_code: str | None,
    expected_status: int,
) -> None:
    """驗證 event 建構或 logger 故障不改變 API 結果並釋放擷取 slot。"""
    log_attempts: list[None] = []

    def fail(*_args: Any, **_kwargs: Any) -> None:
        """模擬不含外洩資料的 logging 依賴故障。"""
        raise RuntimeError("PRIVATE_LOGGING_FAILURE")

    if failure_stage == "event_builder":
        monkeypatch.setattr(routes, "build_event", fail)
    else:
        original_info = routes.logger.info

        def counted_info(*args: Any, **kwargs: Any) -> Any:
            """計算 terminal logging 嘗試次數並轉交既有 logger。"""
            log_attempts.append(None)
            if failure_stage == "logger":
                return fail(*args, **kwargs)
            return original_info(*args, **kwargs)

        monkeypatch.setattr(routes.logger, "info", counted_info)
        if failure_stage == "formatter":
            monkeypatch.setattr(SafeEventFormatter, "format", fail)
        elif failure_stage == "output":

            class FailingStderr:
                """模擬 application stderr 寫入失敗。"""

                def write(self, _message: str) -> int:
                    """拋出不含輸出內容的受控 I/O 例外。"""
                    raise OSError("PRIVATE_STDERR_FAILURE")

                def flush(self) -> None:
                    """模擬 stderr flush 失敗。"""
                    raise OSError("PRIVATE_STDERR_FAILURE")

            monkeypatch.setattr("sns_media_list.logging_config.sys.stderr", FailingStderr())
    extractor = (
        FakeExtractor([record()])
        if error_code is None
        else FixedErrorExtractor(
            error_code,
            "A safe diagnostic.",
            extractor_diagnostics=ExtractorDiagnostics(
                extractor_diagnostic_source="datajob_error",
                extractor_error_type="http_error",
                extractor_exit_code=0,
                extractor_http_statuses=[403],
            ),
        )
    )
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(),
        extractor=extractor,
        token_store=token_store,
    )
    application = create_app(extraction_service=service)
    client = TestClient(application)

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert response.status_code == expected_status
    assert not application.state.limiter._active_extractions
    if failure_stage == "event_builder":
        assert log_attempts == []
    else:
        assert len(log_attempts) == 1
    if error_code is not None:
        assert response.json()["code"] == error_code


@pytest.mark.asyncio
async def test_cancelled_extraction_does_not_log_terminal_event(caplog) -> None:
    """驗證 caller cancellation 不產生 extraction completion 或 failure event。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    extractor = BlockingExtractor()
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    application = create_app(
        extraction_service=ExtractionService(
            Settings(), extractor=extractor, token_store=token_store
        )
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        request_task = asyncio.create_task(
            client.post(
                "/api/extractions",
                json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
            )
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    assert not application.state.limiter._active_extractions
    assert not [
        record
        for record in caplog.records
        if record.name == "sns_media_list"
        and record.msg in {"extraction_complete", "extraction_failed"}
    ]


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
    """驗證 extraction event 不記錄來源 URL、token 或描述文字。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    client, _extractor = make_client([record()])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 200
    events = [record.__dict__.get("event") for record in caplog.records]
    assert events
    event = events[-1]
    assert event["outcome"] == "success"
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["item_count"] == 1
    assert len(events) == 1
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


def test_no_media_returns_422(caplog) -> None:
    """驗證 normalizer no_media response 保持穩定且失敗事件省略 stage。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    client, _extractor = make_client([record(url=None)])

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 422
    assert response.json()["code"] == "no_media"
    events = [
        item.__dict__["event"]
        for item in caplog.records
        if item.name == "sns_media_list" and "event" in item.__dict__
    ]
    assert len(events) == 1
    assert events[0]["reason_code"] == "no_media"
    assert "failure_stage" not in events[0]


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


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        pytest.param("story_auth_required", 403, id="anonymous-auth-required"),
        pytest.param("platform_authentication_failed", 503, id="configured-auth-failed"),
        pytest.param("extraction_failed", 502, id="ambiguous-configured-refusal"),
    ],
)
def test_story_extraction_errors_keep_safe_status_and_issue_no_tokens(
    code: str,
    expected_status: int,
) -> None:
    """驗證 Story 擷取錯誤維持穩定 response headers 且不核發 token。"""
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(),
        extractor=FixedErrorExtractor(code, "A safe diagnostic."),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == code
    assert response.headers["X-SNS-Error-Code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert token_store.size == 0


def test_failure_stage_is_logged_but_not_exposed_by_api_or_openapi(caplog) -> None:
    """驗證 extractor failure stage 僅出現在唯一安全失敗事件。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    token_store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(
        Settings(),
        extractor=FixedErrorExtractor(
            "extraction_failed",
            "A safe public message.",
            failure_stage="extractor_process_unclassified",
        ),
        token_store=token_store,
    )
    client = TestClient(create_app(extraction_service=service))

    response = client.post(
        "/api/extractions",
        json={"url": "https://www.instagram.com/stories/example.user/1111111111111111111/"},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "extraction_failed"
    assert response.json()["message"] == "A safe public message."
    assert response.headers["X-SNS-Error-Code"] == "extraction_failed"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert "failure_stage" not in response.text
    assert "failure_stage" not in " ".join(
        f"{key}:{value}" for key, value in response.headers.items()
    )
    schemas = client.app.openapi().get("components", {}).get("schemas", {})
    assert all("failure_stage" not in schema.get("properties", {}) for schema in schemas.values())

    events = [
        item.__dict__["event"]
        for item in caplog.records
        if item.name == "sns_media_list" and "event" in item.__dict__
    ]
    assert len(events) == 1
    assert events[0]["request_id"] == response.json()["request_id"]
    assert events[0]["reason_code"] == "extraction_failed"
    assert events[0]["failure_stage"] == "extractor_process_unclassified"


def test_active_client_limit_returns_retry_after_and_logs_failure(caplog) -> None:
    """驗證 API 併發限制立即回應、附帶 Retry-After 並只記錄一次。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    client, _extractor = make_client([record()])
    _lease = client.app.state.limiter.acquire_extraction("testclient")

    response = client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "local_rate_limited"
    events = [
        record.__dict__["event"]
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]
    assert len(events) == 1
    assert events[0]["reason_code"] == "local_rate_limited"
    assert events[0]["request_id"] == response.json()["request_id"]
    assert "failure_stage" not in events[0]
