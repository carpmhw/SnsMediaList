"""Tests for the isolated gallery-dl subprocess adapter."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.extractor.gallery_dl import (
    GalleryDlRunner,
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
            "story_unavailable",
            404,
            id="anonymous-story-auth-required",
        ),
        pytest.param(
            "AuthRequired",
            "authenticated cookies needed to access this resource",
            True,
            STORY_URL,
            "story_unavailable",
            404,
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
            True,
            STORY_URL,
            "story_unavailable",
            404,
            id="story-http-401",
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
            "story_unavailable",
            404,
            id="story-http-403",
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
    """Verify pinned structured diagnostics retain stable target-aware errors."""
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
    """Verify AuthenticationError distinguishes configured and anonymous extraction."""
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
    """Verify bounded stderr fallback handles rate limit, challenge, and generic errors."""
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
    ("target_url", "expected_code"),
    [
        pytest.param(STORY_URL, "story_unavailable", id="story"),
        pytest.param(POST_URL, "extraction_failed", id="post"),
    ],
)
async def test_runner_maps_literal_empty_data_job_by_target(
    monkeypatch: pytest.MonkeyPatch,
    target_url: str,
    expected_code: str,
) -> None:
    """Verify only an exact Story treats a literal empty DataJob as unavailable."""
    target = validate_post_url(target_url)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(monkeypatch, b"[]", target, Settings())

    assert exc_info.value.code == expected_code


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
    """Verify valid directory-only and queue-only DataJobs contain no media."""
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        pytest.param(b"", id="empty"),
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
    """Verify the parser accepts only one top-level DataJob event array."""
    target = validate_post_url(STORY_URL)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(monkeypatch, output, target, Settings())

    assert exc_info.value.code == "extraction_failed"
    assert exc_info.value.message == "The extractor returned invalid output."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param([], id="empty-event"),
        pytest.param([99, "https://example.test/unknown", {}], id="unknown-code"),
        pytest.param([-1, "junk"], id="invalid-error"),
        pytest.param([2, "junk"], id="invalid-directory"),
        pytest.param([3, "https://example.test/media"], id="short-url-event"),
        pytest.param([6, 123, {}], id="non-string-queue-url"),
        pytest.param(["3", "https://example.test/media", {}], id="non-integer-code"),
    ],
)
async def test_runner_rejects_malformed_data_job_events(
    monkeypatch: pytest.MonkeyPatch,
    event: list[object],
) -> None:
    """Verify malformed DataJob event codes, lengths, and field types are rejected."""
    target = validate_post_url(STORY_URL)
    with pytest.raises(AppError) as exc_info:
        await extract_process_output(
            monkeypatch,
            json.dumps([event]).encode(),
            target,
            Settings(),
        )

    assert exc_info.value.code == "extraction_failed"


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
    """Verify a stalled extractor is terminated and mapped to timeout."""
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
    assert process.terminated is True


@pytest.mark.asyncio
async def test_runner_rejects_oversized_output(monkeypatch: Any) -> None:
    """Verify extractor output is bounded before parsing."""
    process = FakeProcess(b"x" * 101, returncode=0)

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a fake process with oversized output."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings(extraction_output_limit=100))

    with pytest.raises(AppError) as exc_info:
        await runner.extract(validate_post_url("https://x.com/creator/status/1"))

    assert exc_info.value.code == "extraction_failed"
