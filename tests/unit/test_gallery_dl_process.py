"""Tests for the isolated gallery-dl subprocess adapter."""

import asyncio
import gc
import json
from pathlib import Path
from typing import Any

import pytest

from sns_media_list.config import Settings
from sns_media_list.errors import AppError, ExtractorDiagnostics
from sns_media_list.extractor.gallery_dl import (
    GalleryDlRunner,
    _cleanup_extraction_tasks,
    _stop_extraction_process,
    build_gallery_command,
    build_sanitized_environment,
)
from sns_media_list.extractor.normalizer import normalize_gallery_output
from sns_media_list.url_validation import ValidatedExtractionTarget, validate_post_url

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gallery_dl"
STORY_URL = "https://www.instagram.com/stories/example.user/1111111111111111111/"
POST_URL = "https://www.instagram.com/p/ABC123/"
LOGIN_REDIRECT = "HTTP redirect to login page (https://www.instagram.com/accounts/login/)"
CHALLENGE_REDIRECT = "HTTP redirect to challenge page (https://www.instagram.com/challenge/)"
CONSENT_REDIRECT = "HTTP redirect to consent page (https://www.instagram.com/consent/)"


def test_command_disables_user_config_and_adaptive_delegation() -> None:
    """Verify the command uses only pinned direct-progressive behavior."""
    command = build_gallery_command(
        "https://www.instagram.com/reel/ABC123/",
        proxy_url="http://127.0.0.1:8765",
        timeout_seconds=12,
    )

    assert "--config-ignore" in command
    assert "--no-input" in command
    assert "--no-download" in command
    assert "--whitelist" in command
    assert "instagram,twitter" in command
    assert "extractor.instagram.videos=merged" in command
    assert "extractor.instagram.previews=false" in command
    assert "extractor.twitter.videos=true" in command
    assert "--proxy" in command
    assert "http://127.0.0.1:8765" in command


def test_command_passes_only_matching_instagram_cookie_path() -> None:
    """Verify Instagram authentication uses a path-only category option."""
    command = build_gallery_command(
        "https://www.instagram.com/reel/ABC123/",
        proxy_url="http://127.0.0.1:8765",
        cookie_file="/run/secrets/instagram-cookies.txt",
    )

    assert "extractor.instagram.cookies=/run/secrets/instagram-cookies.txt" in command
    assert "extractor.instagram.cookies-update=false" in command
    assert not any("extractor.twitter.cookies=" in argument for argument in command)
    assert "session-secret" not in " ".join(command)


def test_command_passes_only_matching_x_cookie_path() -> None:
    """Verify X authentication uses the twitter category without Instagram options."""
    command = build_gallery_command(
        "https://x.com/creator/status/1",
        proxy_url="http://127.0.0.1:8765",
        cookie_file="/run/secrets/x-cookies.txt",
    )

    assert "extractor.twitter.cookies=/run/secrets/x-cookies.txt" in command
    assert "extractor.twitter.cookies-update=false" in command
    assert not any("extractor.instagram.cookies=" in argument for argument in command)


def test_command_omits_cookie_options_for_anonymous_extraction() -> None:
    """Verify an omitted platform cookie keeps the extractor anonymous."""
    command = build_gallery_command(
        "https://x.com/creator/status/1",
        proxy_url="http://127.0.0.1:8765",
    )

    assert not any(".cookies=" in argument for argument in command)


def test_environment_removes_inherited_secrets_and_proxies() -> None:
    """Verify subprocess environment cannot use host configuration or secrets."""
    environment = build_sanitized_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/user",
            "HTTP_PROXY": "http://evil-proxy",
            "HTTPS_PROXY": "http://evil-proxy",
            "GALLERY_DL_CONFIG": "/home/user/config.json",
            "COOKIE": "session-secret",
        },
        home="/tmp/gallery-home",
        proxy_url="http://127.0.0.1:8765",
    )

    assert environment["HOME"] == "/tmp/gallery-home"
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:8765"
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:8765"
    assert "GALLERY_DL_CONFIG" not in environment
    assert "COOKIE" not in environment


class FakeProcess:
    """Provide a controllable subprocess for adapter tests."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        """Store subprocess output and termination state."""
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return configured output without launching a child process."""
        return self.stdout, self.stderr

    def terminate(self) -> None:
        """Record a graceful termination request."""
        self.terminated = True

    def kill(self) -> None:
        """Record a forced termination request."""
        self.killed = True

    async def wait(self) -> int:
        """Return the configured process exit status."""
        return self.returncode


class StreamProcess:
    """提供真實 asyncio StreamReader pipe 的 extractor process double。"""

    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        """建立已填入 stdout/stderr 並可被 terminate 喚醒的 process。"""
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()
        self.wait_started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        """若 production path 使用 communicate，立即讓 regression test 失敗。"""
        raise AssertionError("bounded pipe path must not call communicate")

    def terminate(self) -> None:
        """記錄 graceful termination 並完成 process wait。"""
        self.terminated = True
        self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        """記錄 forced termination 並完成 process wait。"""
        self.killed = True
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        """等待 process 被停止並回傳 exit code。"""
        self.wait_started.set()
        await self._exited.wait()
        return self.returncode or 0


class RaisingStreamReader(asyncio.StreamReader):
    """模擬真實 asyncio pipe read 拋出的 raw OSError 或 TimeoutError。"""

    def __init__(self, error: OSError) -> None:
        """保存要由 pipe read 拋出的受控例外。"""
        super().__init__()
        self.error = error

    async def read(self, n: int = -1) -> bytes:
        """拋出 private pipe 例外以驗證 public safe mapping。"""
        _ = n
        raise self.error


class CancellationResistantStopProcess:
    """模擬 terminate wait 取消後仍存在且 kill 後才可釋放的 process。"""

    def __init__(self) -> None:
        """初始化 process cleanup lifecycle 事件。"""
        self.terminated = False
        self.killed = False
        self.wait_started = asyncio.Event()
        self.kill_called = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.wait_finished = asyncio.Event()

    def terminate(self) -> None:
        """記錄 graceful terminate 請求。"""
        self.terminated = True

    def kill(self) -> None:
        """記錄 fallback kill 請求但暫不釋放 process wait。"""
        self.killed = True
        self.kill_called.set()

    async def wait(self) -> int:
        """等待測試釋放後拋出 detached wait 例外。"""
        self.wait_started.set()
        try:
            await self.release_wait.wait()
        finally:
            self.wait_finished.set()
        raise RuntimeError("detached process wait failure")


def load_fixture_records(fixture_name: str) -> list[dict[str, object]]:
    """Load sanitized record fixtures without changing their normalizer format."""
    return [
        json.loads(line)
        for line in (FIXTURES / fixture_name).read_text(encoding="utf-8").splitlines()
    ]


def data_job_output(records: list[dict[str, object]]) -> bytes:
    """Encode media records as a pinned non-JSONL DataJob URL-event array."""
    events: list[list[object]] = []
    for record in records:
        metadata = dict(record)
        url = metadata.pop("url", None)
        if not isinstance(url, str):
            raise ValueError("fixture record requires a URL")
        events.append([3, url, metadata])
    return json.dumps(events).encode()


async def extract_story_fixture(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    target: ValidatedExtractionTarget,
    *,
    record_overrides: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Run one Story record fixture through a pinned DataJob event array."""
    records = load_fixture_records(fixture_name)
    for record in records:
        record.update(record_overrides or {})
    output = data_job_output(records)
    process = FakeProcess(output)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return the Story fixture as successful subprocess output."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return await GalleryDlRunner(Settings()).extract(target)


async def extract_process_output(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    target: ValidatedExtractionTarget,
    settings: Settings,
) -> list[dict[str, object]]:
    """Run configured subprocess output through the extraction adapter."""
    process = FakeProcess(output)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return the configured successful subprocess output."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return await GalleryDlRunner(settings).extract(target)


async def extract_process_stderr(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    target: ValidatedExtractionTarget,
    settings: Settings,
) -> list[dict[str, object]]:
    """Run configured failed subprocess stderr through the extraction adapter."""
    process = FakeProcess(b"", stderr.encode(), returncode=1)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return the configured failed subprocess output."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return await GalleryDlRunner(settings).extract(target)


def instagram_settings(tmp_path: Path, configured: bool) -> Settings:
    """Return anonymous settings or settings backed by a readable Instagram Cookie."""
    if not configured:
        return Settings()
    cookie_file = tmp_path / "instagram.cookies.txt"
    cookie_file.write_text("session-cookie", encoding="utf-8")
    return Settings(instagram_cookie_file=str(cookie_file))


async def assert_runner_error(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_url: str,
    settings: Settings,
    message: str,
    expected_code: str,
    expected_status: int | None = None,
    error_type: str | None = None,
    process_stderr: bool = False,
) -> AppError:
    """Run one diagnostic through the public runner and assert its stable code."""
    target = validate_post_url(target_url)
    with pytest.raises(AppError) as exc_info:
        if process_stderr:
            await extract_process_stderr(monkeypatch, message, target, settings)
        else:
            if error_type is None:
                raise ValueError("structured diagnostics require an error type")
            output = json.dumps([[-1, {"error": error_type, "message": message}]]).encode()
            await extract_process_output(monkeypatch, output, target, settings)

    assert exc_info.value.code == expected_code
    if expected_status is not None:
        assert exc_info.value.status_code == expected_status
    return exc_info.value


@pytest.mark.asyncio
async def test_runner_uses_argument_array_and_parses_data_job_array(monkeypatch: Any) -> None:
    """Verify extraction runs without a shell and parses the pinned DataJob array."""
    output = data_job_output(
        [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "image",
                "url": "https://pbs.twimg.com/media/1.jpg?name=orig",
                "extension": "jpg",
                "progressive": True,
            }
        ]
    )
    process = FakeProcess(output + b"\n")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
        """Capture subprocess invocation details."""
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings(extraction_output_limit=100_000))

    records = await runner.extract(validate_post_url("https://x.com/creator/status/1"))

    assert records[0]["post_id"] == "1"
    assert captured["kwargs"].get("shell", False) is False
    assert captured["args"][0] == "gallery-dl"
    assert "--config-ignore" in captured["args"]


@pytest.mark.asyncio
async def test_runner_makes_one_cookieless_story_attempt_without_cookie_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify anonymous Story extraction launches one cookieless command."""
    output = data_job_output(load_fixture_records("instagram-story-image.jsonl"))
    calls: list[tuple[Any, ...]] = []

    async def fake_create(*args: Any, **_kwargs: Any) -> FakeProcess:
        """Capture the anonymous Story subprocess command."""
        calls.append(args)
        return FakeProcess(output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await GalleryDlRunner(Settings(instagram_cookie_file=None)).extract(
        validate_post_url(STORY_URL)
    )

    assert len(calls) == 1
    assert not any(".cookies=" in argument for argument in calls[0])


@pytest.mark.asyncio
async def test_runner_uses_instagram_cookie_on_only_story_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify authenticated Story extraction uses its cookie on the sole command."""
    cookie_file = tmp_path / "instagram.cookies.txt"
    cookie_file.write_text("session-cookie", encoding="utf-8")
    output = data_job_output(load_fixture_records("instagram-story-image.jsonl"))
    calls: list[tuple[Any, ...]] = []

    async def fake_create(*args: Any, **_kwargs: Any) -> FakeProcess:
        """Capture the authenticated Story subprocess command."""
        calls.append(args)
        return FakeProcess(output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await GalleryDlRunner(Settings(instagram_cookie_file=str(cookie_file))).extract(
        validate_post_url(STORY_URL)
    )

    assert len(calls) == 1
    assert f"extractor.instagram.cookies={cookie_file}" in calls[0]
    assert "extractor.instagram.cookies-update=false" in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "media_id"),
    [
        ("instagram-story-image.jsonl", "1111111111111111111"),
        ("instagram-story-video.jsonl", "2222222222222222222"),
    ],
)
async def test_runner_overwrites_story_records_with_validated_exact_context(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    media_id: str,
) -> None:
    """Verify extractor Story context cannot replace the validated exact target."""
    target = validate_post_url(f"https://www.instagram.com/stories/example.user/{media_id}/")

    records = await extract_story_fixture(
        monkeypatch,
        fixture_name,
        target,
        record_overrides={"platform": "x"},
    )

    assert [(record["platform"], record["post_url"], record["post_id"]) for record in records] == [
        (target.platform, target.canonical_url, target.target_id)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "media_id", "media_type", "extension"),
    [
        ("instagram-story-image.jsonl", "1111111111111111111", "image", "jpg"),
        ("instagram-story-video.jsonl", "2222222222222222222", "video", "mp4"),
    ],
)
async def test_story_normalization_uses_validated_media_id_filename(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    media_id: str,
    media_type: str,
    extension: str,
) -> None:
    """Verify one primary Story item gets a stable exact-media-ID filename."""
    target = validate_post_url(f"https://www.instagram.com/stories/example.user/{media_id}/")
    records = await extract_story_fixture(monkeypatch, fixture_name, target)

    result = normalize_gallery_output(records)

    assert [item.media_type for item in result.items] == [media_type]
    assert [item.filename for item in result.items] == [f"instagram-{media_id}-1.{extension}"]


@pytest.mark.asyncio
async def test_runner_reloads_configured_cookie_file_for_each_process(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Verify each authenticated subprocess receives the current cookie path."""
    cookie_file = tmp_path / "x.cookies.txt"
    cookie_file.write_text("first-session", encoding="utf-8")
    output = data_job_output(
        [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "image",
                "url": "https://pbs.twimg.com/media/1.jpg?name=orig",
                "extension": "jpg",
                "progressive": True,
            }
        ]
    )
    seen_cookie_contents: list[str] = []
    captured_args: list[tuple[Any, ...]] = []

    async def fake_create(*args: Any, **_kwargs: Any) -> FakeProcess:
        """Capture the current cookie file as a child process would read it."""
        captured_args.append(args)
        seen_cookie_contents.append(cookie_file.read_text(encoding="utf-8"))
        return FakeProcess(output + b"\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings(x_cookie_file=str(cookie_file)))
    post_url = validate_post_url("https://x.com/creator/status/1")

    await runner.extract(post_url)
    cookie_file.write_text("second-session", encoding="utf-8")
    await runner.extract(post_url)

    assert seen_cookie_contents == ["first-session", "second-session"]
    assert all(
        f"extractor.twitter.cookies={cookie_file}" in arguments for arguments in captured_args
    )
    assert all(
        "first-session" not in arguments and "second-session" not in arguments
        for arguments in captured_args
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "error_type",
        "message",
        "configured",
        "target_url",
        "expected_code",
        "expected_status",
    ),
    [
        pytest.param(
            "AuthRequired",
            "authenticated cookies needed to access this resource",
            False,
            STORY_URL,
            "story_auth_required",
            403,
            id="anonymous-story-auth-required",
        ),
        pytest.param(
            "AuthRequired",
            "authenticated cookies needed to access this resource",
            True,
            STORY_URL,
            "platform_authentication_failed",
            503,
            id="configured-story-auth-required",
        ),
        pytest.param(
            "AuthRequired",
            "authenticated cookies needed to access this resource",
            True,
            POST_URL,
            "platform_authentication_failed",
            503,
            id="configured-post-auth-required",
        ),
        pytest.param(
            "NotFoundError",
            "Requested story could not be found",
            False,
            STORY_URL,
            "story_unavailable",
            404,
            id="story-not-found",
        ),
        pytest.param(
            "NotFoundError",
            "Requested post could not be found",
            True,
            POST_URL,
            "extraction_failed",
            502,
            id="post-not-found",
        ),
        pytest.param(
            "HttpError",
            f"'401 Unauthorized' for '{STORY_URL}'",
            False,
            STORY_URL,
            "story_auth_required",
            403,
            id="anonymous-story-http-401",
        ),
        pytest.param(
            "HttpError",
            f"'401 Unauthorized' for '{STORY_URL}'",
            True,
            STORY_URL,
            "extraction_failed",
            502,
            id="configured-story-http-401-ambiguous",
        ),
        pytest.param(
            "HttpError",
            f"'401 Unauthorized' for '{POST_URL}'",
            True,
            POST_URL,
            "extraction_failed",
            502,
            id="post-http-401",
        ),
        pytest.param(
            "HttpError",
            f"'403 Forbidden' for '{STORY_URL}'",
            True,
            STORY_URL,
            "extraction_failed",
            502,
            id="configured-story-http-403-ambiguous",
        ),
        pytest.param(
            "HttpError",
            f"'403 Forbidden' for '{STORY_URL}'",
            False,
            STORY_URL,
            "story_auth_required",
            403,
            id="anonymous-story-http-403",
        ),
        pytest.param(
            "HttpError",
            f"'403 Forbidden' for '{POST_URL}'",
            True,
            POST_URL,
            "extraction_failed",
            502,
            id="post-http-403",
        ),
        pytest.param(
            "HttpError",
            f"'404 Not Found' for '{STORY_URL}'",
            True,
            STORY_URL,
            "story_unavailable",
            404,
            id="story-http-404",
        ),
        pytest.param(
            "HttpError",
            f"'404 Not Found' for '{POST_URL}'",
            True,
            POST_URL,
            "extraction_failed",
            502,
            id="post-http-404",
        ),
        pytest.param(
            "HttpError",
            f"'429 Too Many Requests' for '{POST_URL}'",
            True,
            POST_URL,
            "upstream_rate_limited",
            429,
            id="post-http-429",
        ),
        pytest.param(
            "AbortExtraction",
            LOGIN_REDIRECT,
            True,
            STORY_URL,
            "platform_authentication_failed",
            503,
            id="configured-story-login-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            LOGIN_REDIRECT,
            True,
            POST_URL,
            "platform_authentication_failed",
            503,
            id="configured-post-login-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            CHALLENGE_REDIRECT,
            True,
            STORY_URL,
            "platform_authentication_failed",
            503,
            id="configured-story-challenge-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            CHALLENGE_REDIRECT,
            True,
            POST_URL,
            "platform_authentication_failed",
            503,
            id="configured-post-challenge-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            CONSENT_REDIRECT,
            True,
            STORY_URL,
            "platform_authentication_failed",
            503,
            id="configured-story-consent-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            LOGIN_REDIRECT,
            False,
            POST_URL,
            "post_unavailable",
            404,
            id="anonymous-post-login-redirect",
        ),
        pytest.param(
            "AbortExtraction",
            "creator's posts are private",
            False,
            POST_URL,
            "post_unavailable",
            404,
            id="anonymous-private-post",
        ),
        pytest.param(
            "AbortExtraction",
            "Unsupported with GraphQL API",
            False,
            POST_URL,
            "extraction_failed",
            502,
            id="generic-abort",
        ),
    ],
)
async def test_runner_maps_pinned_structured_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_type: str,
    message: str,
    configured: bool,
    target_url: str,
    expected_code: str,
    expected_status: int,
) -> None:
    """驗證 gallery-dl 結構化診斷維持穩定且依目標分類的錯誤。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=target_url,
        settings=instagram_settings(tmp_path, configured),
        error_type=error_type,
        message=message,
        expected_code=expected_code,
        expected_status=expected_status,
    )

    assert message not in error.message
    assert "session-cookie" not in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "target_url", "expected_code"),
    [
        pytest.param(True, STORY_URL, "platform_authentication_failed", id="configured-story"),
        pytest.param(False, STORY_URL, "story_auth_required", id="anonymous-story"),
        pytest.param(False, POST_URL, "post_unavailable", id="anonymous-post"),
    ],
)
async def test_runner_maps_structured_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: bool,
    target_url: str,
    expected_code: str,
) -> None:
    """驗證 AuthenticationError 依 Cookie 配置狀態區分錯誤。"""
    await assert_runner_error(
        monkeypatch,
        target_url=target_url,
        settings=instagram_settings(tmp_path, configured),
        error_type="AuthenticationError",
        message="Invalid login credentials",
        expected_code=expected_code,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "configured", "target_url", "expected_code"),
    [
        pytest.param(
            f"HttpError: '429 Too Many Requests' for '{STORY_URL}'",
            True,
            STORY_URL,
            "upstream_rate_limited",
            id="http-429-precedes-auth",
        ),
        pytest.param(
            "AuthRequired: authenticated cookies needed",
            False,
            STORY_URL,
            "story_auth_required",
            id="anonymous-story-auth-required-stderr",
        ),
        pytest.param(
            "AuthRequired: authenticated cookies needed",
            True,
            STORY_URL,
            "platform_authentication_failed",
            id="configured-story-auth-required-stderr",
        ),
        pytest.param(
            f"HttpError: '401 Unauthorized' for '{STORY_URL}'",
            True,
            STORY_URL,
            "extraction_failed",
            id="configured-story-http-401-stderr",
        ),
        pytest.param(
            f"HttpError: '404 Not Found' for '{STORY_URL}'",
            True,
            STORY_URL,
            "story_unavailable",
            id="configured-story-http-404-stderr",
        ),
        pytest.param(
            f"AbortExtraction: {CHALLENGE_REDIRECT}",
            True,
            STORY_URL,
            "platform_authentication_failed",
            id="configured-challenge-redirect",
        ),
        pytest.param(
            "unexpected upstream details",
            False,
            POST_URL,
            "extraction_failed",
            id="generic-process-error",
        ),
    ],
)
async def test_runner_maps_process_stderr_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    configured: bool,
    target_url: str,
    expected_code: str,
) -> None:
    """驗證受限 stderr fallback 分類 rate limit、challenge 與一般錯誤。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=target_url,
        settings=instagram_settings(tmp_path, configured),
        message=message,
        expected_code=expected_code,
        process_stderr=True,
    )

    assert message not in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "configured", "expected_code"),
    [
        pytest.param(
            f"HttpError: '429 Too Many Requests' and AuthRequired for '{STORY_URL}'",
            True,
            "upstream_rate_limited",
            id="http-429-precedes-auth-and-not-found",
        ),
        pytest.param(
            f"HttpError: '404 Not Found' and login required for '{STORY_URL}'",
            False,
            "story_unavailable",
            id="http-404-precedes-auth",
        ),
        pytest.param(
            f"HTTP 403 for '{STORY_URL}?reason=invalid+session'",
            True,
            "extraction_failed",
            id="query-auth-phrase-is-not-evidence",
        ),
        pytest.param(
            f"HTTP 403 for '{STORY_URL}' expired session",
            True,
            "platform_authentication_failed",
            id="explicit-session-evidence-precedes-ambiguous-status",
        ),
        pytest.param(
            f"HTTP 403 for '{STORY_URL}' Story has expired",
            True,
            "story_unavailable",
            id="explicit-availability-evidence",
        ),
        pytest.param(
            "generic authentication subsystem failure",
            False,
            "extraction_failed",
            id="generic-authentication-word-is-not-explicit-evidence",
        ),
    ],
)
async def test_story_error_classifier_uses_evidence_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    configured: bool,
    expected_code: str,
) -> None:
    """驗證 Story 錯誤分類依優先順序使用 URL 外的明確證據。"""
    await assert_runner_error(
        monkeypatch,
        target_url=STORY_URL,
        settings=instagram_settings(tmp_path, configured),
        message=message,
        expected_code=expected_code,
        process_stderr=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "source",
        "error_type",
        "message",
        "configured",
        "target_url",
        "expected_code",
        "expected_stage",
    ),
    [
        pytest.param(
            "stderr",
            None,
            "unrecognized extractor diagnostic",
            False,
            STORY_URL,
            "extraction_failed",
            "extractor_process_unclassified",
            id="unknown-stderr",
        ),
        pytest.param(
            "record",
            "UnknownError",
            "unrecognized extractor diagnostic",
            False,
            STORY_URL,
            "extraction_failed",
            "extractor_process_unclassified",
            id="unknown-datajob-error",
        ),
        pytest.param(
            "stderr",
            None,
            "AuthRequired: authenticated cookies needed",
            False,
            STORY_URL,
            "story_auth_required",
            "extractor_platform_error",
            id="anonymous-story-auth-stderr",
        ),
        pytest.param(
            "record",
            "AuthRequired",
            "authenticated cookies needed",
            False,
            STORY_URL,
            "story_auth_required",
            "extractor_platform_error",
            id="anonymous-story-auth-record",
        ),
        pytest.param(
            "stderr",
            None,
            "AuthenticationError: login required",
            True,
            STORY_URL,
            "platform_authentication_failed",
            "extractor_platform_error",
            id="configured-story-auth-stderr",
        ),
        pytest.param(
            "record",
            "AuthenticationError",
            "login required",
            True,
            STORY_URL,
            "platform_authentication_failed",
            "extractor_platform_error",
            id="configured-story-auth-record",
        ),
        pytest.param(
            "stderr",
            None,
            "NotFoundError",
            False,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-not-found-stderr",
        ),
        pytest.param(
            "record",
            "NotFoundError",
            "requested Story could not be found",
            True,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-not-found-record",
        ),
        pytest.param(
            "stderr",
            None,
            "HTTP 404 Not Found",
            True,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-404-stderr",
        ),
        pytest.param(
            "record",
            "HttpError",
            "HTTP 404 Not Found",
            False,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-404-record",
        ),
        pytest.param(
            "stderr",
            None,
            "HTTP 429 Too Many Requests AuthRequired",
            True,
            STORY_URL,
            "upstream_rate_limited",
            "extractor_platform_error",
            id="rate-limit-stderr-priority",
        ),
        pytest.param(
            "record",
            "HttpError",
            "HTTP 429 Too Many Requests AuthRequired",
            False,
            STORY_URL,
            "upstream_rate_limited",
            "extractor_platform_error",
            id="rate-limit-record-priority",
        ),
        pytest.param(
            "stderr",
            None,
            "Story has expired",
            False,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-availability-stderr",
        ),
        pytest.param(
            "record",
            "UnknownError",
            "Story has expired",
            True,
            STORY_URL,
            "story_unavailable",
            "extractor_platform_error",
            id="story-availability-record",
        ),
        pytest.param(
            "stderr",
            None,
            "HTTP 401 Unauthorized",
            True,
            STORY_URL,
            "extraction_failed",
            "extractor_process_unclassified",
            id="configured-401-stderr",
        ),
        pytest.param(
            "record",
            "HttpError",
            "HTTP 403 Forbidden",
            True,
            STORY_URL,
            "extraction_failed",
            "extractor_process_unclassified",
            id="configured-403-record",
        ),
        pytest.param(
            "stderr",
            None,
            "AuthRequired: authenticated cookies needed",
            False,
            POST_URL,
            "post_unavailable",
            "extractor_platform_error",
            id="post-auth-anonymous-stderr",
        ),
        pytest.param(
            "record",
            "AuthenticationError",
            "AuthenticationError",
            True,
            "https://www.instagram.com/reel/ABC123/",
            "platform_authentication_failed",
            "extractor_platform_error",
            id="reel-configured-auth-record",
        ),
        pytest.param(
            "stderr",
            None,
            "AuthRequired: login required",
            False,
            "https://x.com/creator/status/1",
            "post_unavailable",
            "extractor_platform_error",
            id="x-auth-anonymous-stderr",
        ),
        pytest.param(
            "stderr",
            None,
            f"HTTP 403 for '{STORY_URL}?reason=invalid+session",
            True,
            STORY_URL,
            "extraction_failed",
            "extractor_process_unclassified",
            id="url-query-is-not-evidence",
        ),
    ],
)
async def test_runner_classifies_both_error_sources_with_failure_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    error_type: str | None,
    message: str,
    configured: bool,
    target_url: str,
    expected_code: str,
    expected_stage: str,
) -> None:
    """驗證 stderr 與合法 DataJob error 共用 reason／stage 分類與優先序。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=target_url,
        settings=instagram_settings(tmp_path, configured),
        message=message,
        expected_code=expected_code,
        error_type=error_type,
        process_stderr=source == "stderr",
    )

    assert error.failure_stage == expected_stage


@pytest.mark.asyncio
async def test_nonzero_stderr_records_bounded_diagnostic_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證 stderr 診斷只輸出固定欄位、實際退出碼與明確狀態。"""
    stderr = (
        b"unclassified response COOKIE_SENTINEL HTTP 403 Forbidden "
        b"https://example.test/private?token=URL_SENTINEL"
    )
    process = FakeProcess(b"", stderr, returncode=7)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """回傳帶有合成敏感 stderr 與實際非零退出碼的程序替身。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(AppError) as exc_info:
        await GalleryDlRunner(Settings()).extract(validate_post_url(POST_URL))

    error = exc_info.value
    assert error.code == "extraction_failed"
    assert error.failure_stage == "extractor_process_unclassified"
    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics == ExtractorDiagnostics(
        extractor_diagnostic_source="stderr",
        extractor_error_type="unknown",
        extractor_exit_code=7,
        extractor_http_statuses=[403],
    )
    assert "COOKIE_SENTINEL" not in repr(error.extractor_diagnostics)
    assert "URL_SENTINEL" not in repr(error.extractor_diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_error_type", "diagnostic_type", "expected_code"),
    [
        pytest.param("  AUTHREQUIRED  ", "auth_required", "post_unavailable", id="auth-required"),
        pytest.param("AuthenticationError", "authentication_error", "post_unavailable"),
        pytest.param("AuthorizationError", "authorization_error", "extraction_failed"),
        pytest.param("NotFoundError", "not_found", "extraction_failed"),
        pytest.param("HttpError", "http_error", "extraction_failed"),
        pytest.param("ChallengeError", "challenge_error", "extraction_failed"),
        pytest.param("ExtractionError", "extraction_error", "extraction_failed"),
        pytest.param("NoExtractorError", "no_extractor", "extraction_failed"),
    ],
)
async def test_datajob_error_records_use_exact_diagnostic_type_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    raw_error_type: str,
    diagnostic_type: str,
    expected_code: str,
) -> None:
    """驗證 DataJob 錯誤類別依精確 strip／casefold allowlist 正規化。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=POST_URL,
        settings=Settings(),
        message="unclassified diagnostic",
        error_type=raw_error_type,
        expected_code=expected_code,
    )

    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_diagnostic_source == "datajob_error"
    assert error.extractor_diagnostics.extractor_error_type == diagnostic_type
    assert error.extractor_diagnostics.extractor_exit_code == 0
    assert error.extractor_diagnostics.extractor_http_statuses is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_error_type",
    ["UnknownError", "AuthRequiredExtra", "PrefixAuthRequired", "HttpErrorExtra"],
)
async def test_datajob_similar_error_type_names_use_unknown_fallback(
    monkeypatch: pytest.MonkeyPatch,
    raw_error_type: str,
) -> None:
    """驗證未知或相似 prefix 類別不會被 substring 推測。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=POST_URL,
        settings=Settings(),
        message="unclassified diagnostic",
        error_type=raw_error_type,
        expected_code="extraction_failed",
    )

    assert error.failure_stage == "extractor_process_unclassified"
    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_diagnostic_source == "datajob_error"
    assert error.extractor_diagnostics.extractor_error_type == "unknown"
    assert error.extractor_diagnostics.extractor_exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_status", "expected_code"),
    [
        pytest.param("HTTP 401 Unauthorized", (401,), "extraction_failed", id="401"),
        pytest.param("status: 403 Forbidden", (403,), "extraction_failed", id="403"),
        pytest.param("HTTP/2 404 Not Found", (404,), "story_unavailable", id="404"),
        pytest.param("HTTP 429 Too Many Requests", (429,), "upstream_rate_limited", id="429"),
    ],
)
async def test_datajob_diagnostics_records_explicit_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    expected_status: tuple[int, ...],
    expected_code: str,
) -> None:
    """驗證四種明確上游 HTTP 狀態留在診斷欄位並維持既有分類。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=STORY_URL,
        settings=instagram_settings(tmp_path, configured=True),
        message=message,
        error_type="HttpError",
        expected_code=expected_code,
    )

    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_http_statuses == expected_status
    assert error.extractor_diagnostics.extractor_exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["datajob_error", "stderr"])
@pytest.mark.parametrize(
    ("message", "expected_status"),
    [
        ("'500 Internal Server Error' for 'https://example.test/private?token=SECRET'", (500,)),
        ("'502 Bad Gateway' for 'https://example.test/private?token=SECRET'", (502,)),
        ("'503 Service Unavailable' for 'https://example.test/private?token=SECRET'", (503,)),
        ("'504 Gateway Timeout' for 'https://example.test/private?token=SECRET'", (504,)),
        ("'500 internal server error' for 'https://example.test/private?token=SECRET'", (500,)),
        ("503 Service Unavailable; 500 Internal Server Error; 503 Service Unavailable", (500, 503)),
    ],
)
async def test_runner_records_server_error_reason_phrases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    message: str,
    expected_status: tuple[int, ...],
) -> None:
    """驗證兩種錯誤來源辨識明確 5xx 片語，保留公開分類且不洩漏原文。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=STORY_URL,
        settings=instagram_settings(tmp_path, configured=True),
        message=message,
        error_type="HttpError",
        expected_code="extraction_failed",
        process_stderr=source == "stderr",
    )

    assert error.status_code == 502
    assert error.failure_stage == "extractor_process_unclassified"
    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_diagnostic_source == source
    assert error.extractor_diagnostics.extractor_http_statuses == expected_status
    assert "SECRET" not in repr(error.extractor_diagnostics)
    assert "example.test" not in repr(error.extractor_diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "403",
        "500",
        "Story ID 123500123",
        "500 Bad Gateway",
        "1500 Internal Server Error",
        "500 Internal Server Errorish",
        "only inside https://example.test/path?query=HTTP%20403%20Forbidden",
        "only inside https://example.test/path?query=500%20Internal%20Server%20Error",
        "only inside https://example.test/500/Internal/Server/Error",
    ],
)
async def test_datajob_diagnostics_ignores_bare_and_url_only_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
) -> None:
    """驗證裸數字與已移除 URL 內的狀態樣式不形成證據。"""
    error = await assert_runner_error(
        monkeypatch,
        target_url=STORY_URL,
        settings=instagram_settings(tmp_path, configured=True),
        message=message,
        error_type="HttpError",
        expected_code="extraction_failed",
    )

    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_http_statuses is None


@pytest.mark.asyncio
async def test_http_status_diagnostics_are_bounded_without_changing_reason_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """驗證診斷最多保留八個狀態但分類仍查看完整 bounded 訊息。"""
    message = " ".join(
        [
            *(f"HTTP {status}" for status in range(401, 409)),
            "HTTP 429 Too Many Requests",
            "500 Internal Server Error",
            "503 Service Unavailable",
            "HTTP 401",
        ]
    )
    error = await assert_runner_error(
        monkeypatch,
        target_url=STORY_URL,
        settings=instagram_settings(tmp_path, configured=True),
        message=message,
        error_type="HttpError",
        expected_code="upstream_rate_limited",
    )

    assert error.failure_stage == "extractor_platform_error"
    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_http_statuses == tuple(range(401, 409))


@pytest.mark.asyncio
async def test_multiple_datajob_errors_use_only_the_first_error_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證後續 DataJob error 不會覆寫第一筆的分類或狀態證據。"""
    events = [
        [-1, {"error": "HttpError", "message": "HTTP 403 Forbidden"}],
        [-1, {"error": "HttpError", "message": "HTTP 429 Too Many Requests"}],
    ]
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(
            monkeypatch,
            json.dumps(events).encode(),
            validate_post_url(POST_URL),
            Settings(),
        )

    error = exc_info.value
    assert error.code == "extraction_failed"
    assert error.extractor_diagnostics is not None
    assert error.extractor_diagnostics.extractor_error_type == "http_error"
    assert error.extractor_diagnostics.extractor_http_statuses == (403,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_url", "expected_code"),
    [
        pytest.param(STORY_URL, "story_unavailable", id="story"),
        pytest.param(POST_URL, "extraction_failed", id="post"),
        pytest.param(
            "https://www.instagram.com/reel/ABC123/",
            "extraction_failed",
            id="reel",
        ),
        pytest.param("https://x.com/creator/status/1", "extraction_failed", id="x"),
    ],
)
async def test_runner_maps_literal_empty_data_job_by_target(
    monkeypatch: pytest.MonkeyPatch,
    target_url: str,
    expected_code: str,
) -> None:
    """驗證 literal 空 DataJob 依 Story 與其他目標標記 empty-output stage。"""
    target = validate_post_url(target_url)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(monkeypatch, b"[]", target, Settings())

    assert exc_info.value.code == expected_code
    assert exc_info.value.failure_stage == "extractor_empty_output"
    assert exc_info.value.extractor_diagnostics == ExtractorDiagnostics(extractor_exit_code=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param([2, {"category": "instagram"}], id="directory"),
        pytest.param([6, "https://www.instagram.com/stories/example.user/", {}], id="queue"),
    ],
)
async def test_runner_maps_non_media_data_job_to_no_media(
    monkeypatch: pytest.MonkeyPatch,
    event: list[object],
) -> None:
    """驗證合法 directory-only 與 queue-only DataJob 標記 no-media stage。"""
    target = validate_post_url(STORY_URL)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(
            monkeypatch,
            json.dumps([event]).encode(),
            target,
            Settings(),
        )

    assert exc_info.value.code == "no_media"
    assert exc_info.value.status_code == 422
    assert exc_info.value.failure_stage == "extractor_no_media"
    assert exc_info.value.extractor_diagnostics == ExtractorDiagnostics(extractor_exit_code=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b" \n\t", id="whitespace-only"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"not-json", id="non-json"),
        pytest.param(
            json.dumps(
                {
                    "num": 1,
                    "type": "image",
                    "url": "https://scontent.fixture.cdninstagram.com/direct.jpg",
                }
            ).encode(),
            id="direct-record",
        ),
        pytest.param(
            json.dumps(
                [3, "https://scontent.fixture.cdninstagram.com/direct.jpg", {"num": 1}]
            ).encode(),
            id="direct-event",
        ),
        pytest.param(
            (
                json.dumps([2, {"category": "instagram"}])
                + "\n"
                + json.dumps([3, "https://scontent.fixture.cdninstagram.com/jsonl.jpg", {"num": 1}])
            ).encode(),
            id="jsonl-events",
        ),
    ],
)
async def test_runner_rejects_non_data_job_output(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    """驗證 parser 僅接受一個合法的頂層 DataJob event array。"""
    target = validate_post_url(STORY_URL)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(monkeypatch, output, target, Settings())

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.message == "The extractor returned invalid output."
    assert exc_info.value.failure_stage == "extractor_invalid_output"
    assert exc_info.value.extractor_diagnostics == ExtractorDiagnostics(extractor_exit_code=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param([], id="empty-event"),
        pytest.param([99, "https://example.test/unknown", {}], id="unknown-code"),
        pytest.param([-1, "junk"], id="invalid-error"),
        pytest.param([-1, {"error": 1, "message": "invalid metadata"}], id="invalid-error-type"),
        pytest.param([-1, {"error": "UnknownError", "message": 2}], id="invalid-message-type"),
        pytest.param([2, "junk"], id="invalid-directory"),
        pytest.param([3, "https://example.test/media"], id="short-url-event"),
        pytest.param([3, 123, {}], id="non-string-url"),
        pytest.param([6, 123, {}], id="non-string-queue-url"),
        pytest.param([6, "https://x.com/creator/status/1", "metadata"], id="invalid-queue-data"),
        pytest.param(["3", "https://example.test/media", {}], id="non-integer-code"),
    ],
)
async def test_runner_rejects_malformed_data_job_events(
    monkeypatch: pytest.MonkeyPatch,
    event: list[object],
) -> None:
    """驗證畸形 DataJob event code、長度及欄位型別標記 invalid-output stage。"""
    target = validate_post_url(STORY_URL)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(
            monkeypatch,
            json.dumps([event]).encode(),
            target,
            Settings(),
        )

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_code", "expected_stage"),
    [
        pytest.param(
            [
                [3, "https://cdn.example.test/story.jpg", {"type": "image"}],
                [-1, {"error": "AuthRequired", "message": "authenticated cookies needed"}],
            ],
            "story_auth_required",
            "extractor_platform_error",
            id="media-plus-known-error",
        ),
        pytest.param(
            [[2, {"category": "instagram"}], [-1, {"error": "UnknownError", "message": "unknown"}]],
            "extraction_failed",
            "extractor_process_unclassified",
            id="non-media-plus-unknown-error",
        ),
        pytest.param(
            [
                [3, "https://cdn.example.test/story.jpg", {"type": "image"}],
                [-1, {"error": "UnknownError", "message": "unknown"}],
            ],
            "extraction_failed",
            "extractor_process_unclassified",
            id="media-plus-unknown-error",
        ),
        pytest.param(
            [
                [3, "https://cdn.example.test/story.jpg", {"type": "image"}],
                [-1, {"error": "AuthRequired", "message": 123}],
            ],
            "extraction_failed",
            "extractor_invalid_output",
            id="media-plus-malformed-error-metadata",
        ),
    ],
)
async def test_runner_maps_error_records_before_media_and_non_media_records(
    monkeypatch: pytest.MonkeyPatch,
    events: list[list[object]],
    expected_code: str,
    expected_stage: str,
) -> None:
    """驗證合法 error record 優先於媒體與 non-media 結果且保留精確 stage。"""
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(
            monkeypatch,
            json.dumps(events).encode(),
            validate_post_url(STORY_URL),
            Settings(),
        )

    assert exc_info.value.code == expected_code
    assert exc_info.value.failure_stage == expected_stage


@pytest.mark.asyncio
async def test_runner_accepts_pinned_directory_url_and_queue_message_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify valid directory, URL, and queue events retain the record contract."""
    media_url = "https://pbs.twimg.com/media/1.jpg?name=orig"
    output = json.dumps(
        [
            [2, {"event": "directory"}],
            [3, media_url, {"event": "url"}],
            [6, "https://x.com/creator/status/2", {"event": "queue"}],
        ]
    ).encode()
    target = validate_post_url("https://x.com/creator/status/1")

    records = await extract_process_output(monkeypatch, output, target, Settings())

    assert [record["event"] for record in records] == ["url"]
    assert records[0]["url"] == media_url


@pytest.mark.asyncio
async def test_runner_normalizes_gallery_message_tuples(monkeypatch: Any) -> None:
    """Verify directory metadata is skipped and URL events receive post context."""
    output = json.dumps(
        [
            [
                2,
                {
                    "category": "twitter",
                    "content": "caption",
                    "tweet_id": "2078132868937912695",
                    "author": {"name": "ten_sura_anime"},
                },
            ],
            [
                3,
                "https://pbs.twimg.com/media/HNUlNsMaAAAebWz?format=jpg&name=orig",
                {
                    "author": {"name": "ten_sura_anime"},
                    "content": "caption",
                    "num": 1,
                    "type": "photo",
                    "extension": "jpg",
                    "width": 849,
                    "height": 1200,
                },
            ],
        ]
    ).encode()
    process = FakeProcess(output + b"\n")

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a successful process containing gallery message tuples."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings())

    records = await runner.extract(
        validate_post_url("https://x.com/ten_sura_anime/status/2078132868937912695")
    )

    assert len(records) == 1
    assert records[0]["platform"] == "x"
    assert records[0]["post_url"] == "https://x.com/ten_sura_anime/status/2078132868937912695/"
    assert records[0]["post_id"] == "2078132868937912695"
    assert records[0]["url"] == "https://pbs.twimg.com/media/HNUlNsMaAAAebWz?format=jpg&name=orig"
    assert records[0]["author"] == "ten_sura_anime"
    assert records[0]["description"] == "caption"


@pytest.mark.asyncio
async def test_runner_maps_nonzero_exit_to_safe_error(monkeypatch: Any) -> None:
    """Verify extractor stderr is not returned in the public error."""
    process = FakeProcess(b"", b"unexpected upstream details", returncode=1)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a failed fake process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings())

    with pytest.raises(AppError) as exc_info:
        await runner.extract(validate_post_url("https://x.com/creator/status/1"))

    assert exc_info.value.code == "extraction_failed"
    assert "private" not in exc_info.value.message


@pytest.mark.asyncio
async def test_runner_terminates_on_timeout(monkeypatch: Any) -> None:
    """驗證 test-double communicate 超時後終止 extractor 並標記 timeout stage。"""
    process = FakeProcess(b"", returncode=0)

    async def slow_communicate() -> tuple[bytes, bytes]:
        """Keep the fake process pending beyond the configured deadline."""
        await asyncio.sleep(1)
        return b"", b""

    process.communicate = slow_communicate  # type: ignore[method-assign]

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a stalled fake process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings(extraction_timeout_seconds=0.01))

    with pytest.raises(AppError) as exc_info:
        await runner.extract(validate_post_url("https://x.com/creator/status/1"))

    assert exc_info.value.code == "extraction_timeout"
    assert exc_info.value.failure_stage == "extractor_timeout"
    assert exc_info.value.extractor_diagnostics is None
    assert process.terminated is True


@pytest.mark.asyncio
async def test_runner_maps_test_double_communicate_io_error_to_io_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證 test-double communicate I/O 失敗附帶安全 io stage。"""
    process = FakeProcess(b"", returncode=0)

    async def fail_communicate() -> tuple[bytes, bytes]:
        """以 raw OSError 模擬 communicate fallback 的 pipe 讀取失敗。"""
        raise OSError("PRIVATE_COMMUNICATE_FAILURE")

    process.communicate = fail_communicate  # type: ignore[method-assign]

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """回傳 communicate fallback 會發生 I/O 失敗的程序 double。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await GalleryDlRunner(Settings()).extract(
            validate_post_url("https://x.com/creator/status/1")
        )

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_io"
    assert exc_info.value.extractor_diagnostics is None
    assert process.terminated is True


@pytest.mark.asyncio
async def test_runner_rejects_oversized_output(monkeypatch: Any) -> None:
    """驗證 communicate 回傳後的 stdout 大小防線附帶 output-limit stage。"""
    process = FakeProcess(b"x" * 101, returncode=0)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a fake process with oversized output."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings(extraction_output_limit=100))

    with pytest.raises(AppError) as exc_info:
        await runner.extract(validate_post_url("https://x.com/creator/status/1"))

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_output_limit"
    assert exc_info.value.extractor_diagnostics is None


@pytest.mark.asyncio
async def test_runner_terminates_real_pipe_when_stdout_exceeds_limit(monkeypatch: Any) -> None:
    """真實 asyncio pipe stdout 超限時應立即終止 process 並回傳安全錯誤。"""
    process = StreamProcess(b"x" * 101, b"diagnostics" * 1000)

    async def fake_create(*_args: Any, **_kwargs: Any) -> StreamProcess:
        """回傳帶有 bounded pipe stream 的 process double。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await GalleryDlRunner(Settings(extraction_output_limit=100)).extract(
            validate_post_url("https://x.com/creator/status/1")
        )

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_output_limit"
    assert exc_info.value.extractor_diagnostics is None
    assert process.terminated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipe_name", "pipe_error"),
    [
        pytest.param("stdout", OSError("private stdout pipe detail"), id="stdout-os-error"),
        pytest.param("stdout", TimeoutError("private stdout timeout detail"), id="stdout-timeout"),
        pytest.param("stderr", OSError("private stderr pipe detail"), id="stderr-os-error"),
        pytest.param("stderr", TimeoutError("private stderr timeout detail"), id="stderr-timeout"),
    ],
)
async def test_runner_maps_raw_pipe_errors_to_safe_extraction_failure(
    monkeypatch: pytest.MonkeyPatch,
    pipe_name: str,
    pipe_error: OSError,
) -> None:
    """真實 pipe 的 raw 讀取例外應安全映射且完成 bounded process cleanup。"""
    process = StreamProcess(b"", b"")
    setattr(process, pipe_name, RaisingStreamReader(pipe_error))

    async def fake_create(*_args: Any, **_kwargs: Any) -> StreamProcess:
        """回傳會在 stdout read 失敗的 bounded pipe process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await GalleryDlRunner(Settings()).extract(
            validate_post_url("https://x.com/creator/status/1")
        )

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_io"
    assert exc_info.value.extractor_diagnostics is None
    assert exc_info.value.message == "The extractor output could not be read."
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(pipe_error) not in exc_info.value.message
    assert process.terminated is True


@pytest.mark.asyncio
async def test_runner_maps_spawn_oserror_to_safe_extraction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證 subprocess 啟動 OSError 映射為安全錯誤及 extractor_start stage。"""

    async def fake_create(*_args: Any, **_kwargs: Any) -> StreamProcess:
        """模擬 gallery-dl process 無法啟動的 raw spawn 例外。"""
        raise OSError("private spawn detail")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await GalleryDlRunner(Settings()).extract(
            validate_post_url("https://x.com/creator/status/1")
        )

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.failure_stage == "extractor_start"
    assert exc_info.value.extractor_diagnostics is None
    assert exc_info.value.message == "The extractor process could not be started."
    assert isinstance(exc_info.value.__cause__, OSError)
    assert "private spawn detail" not in exc_info.value.message


@pytest.mark.asyncio
async def test_cleanup_extraction_tasks_observes_done_failure_when_cancelled() -> None:
    """extractor cleanup 被取消時仍應消耗已完成 failed task 的例外。"""
    release_pending = asyncio.Event()
    pending_cancelled = asyncio.Event()

    async def fail_task() -> None:
        """建立尚未被 await 的已完成 extractor task 例外。"""
        raise RuntimeError("completed extraction failure")

    async def cancellation_resistant_task() -> None:
        """收到取消後等待釋放，再拋出供 detached observer 消耗的例外。"""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pending_cancelled.set()
            await release_pending.wait()
        raise RuntimeError("pending extraction failure")

    completed_task = asyncio.create_task(fail_task())
    await asyncio.wait({completed_task})
    pending_task = asyncio.create_task(cancellation_resistant_task())
    cleanup_task = asyncio.create_task(_cleanup_extraction_tasks([completed_task, pending_task]))
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 extractor cleanup observer 消耗的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        await asyncio.wait_for(pending_cancelled.wait(), timeout=0.1)
        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task
        release_pending.set()
        with pytest.raises(RuntimeError, match="pending extraction failure"):
            await asyncio.wait_for(asyncio.shield(pending_task), timeout=0.1)
        del completed_task
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        release_pending.set()
        if not pending_task.done():
            pending_task.cancel()
        await asyncio.gather(pending_task, return_exceptions=True)
        if not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_stop_extraction_process_kills_after_repeated_cancellation() -> None:
    """terminate wait 連續收到取消時仍應 kill 並保留 caller cancellation。"""
    process = CancellationResistantStopProcess()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 process wait observer 消耗的 detached 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    stop_task = asyncio.create_task(_stop_extraction_process(process))
    try:
        await asyncio.wait_for(process.wait_started.wait(), timeout=0.1)
        stop_task.cancel()
        await asyncio.wait_for(process.kill_called.wait(), timeout=0.1)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        process.release_wait.set()
        await asyncio.wait_for(process.wait_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert process.killed is True
        assert unhandled == []
    finally:
        process.release_wait.set()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        if not process.wait_finished.is_set():
            await asyncio.wait_for(process.wait_finished.wait(), timeout=0.1)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_runner_cancellation_is_propagated_without_cleanup_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證 caller cancellation 清理 process 後仍原樣傳遞而不生成診斷錯誤。"""
    process = StreamProcess(b"", b"")

    async def fake_create(*_args: Any, **_kwargs: Any) -> StreamProcess:
        """回傳可觀察 wait 與 terminate cleanup 的程序替身。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    extraction = asyncio.create_task(
        GalleryDlRunner(Settings()).extract(validate_post_url(POST_URL))
    )
    await asyncio.wait_for(process.wait_started.wait(), timeout=0.1)
    extraction.cancel()

    with pytest.raises(asyncio.CancelledError):
        await extraction

    assert process.terminated is True
    assert process.killed is False
    assert process.returncode == -15
