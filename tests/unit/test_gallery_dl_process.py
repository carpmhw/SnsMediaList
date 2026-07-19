"""Tests for the isolated gallery-dl subprocess adapter."""

import asyncio
import json
from typing import Any

import pytest

from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.extractor.gallery_dl import (
    GalleryDlRunner,
    build_gallery_command,
    build_sanitized_environment,
)
from sns_media_list.url_validation import validate_post_url


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


@pytest.mark.asyncio
async def test_runner_uses_argument_array_and_parses_json_lines(monkeypatch: Any) -> None:
    """Verify extraction runs without a shell and returns parsed records."""
    output = json.dumps(
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
    ).encode()
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
async def test_runner_maps_json_login_error_to_post_unavailable(monkeypatch: Any) -> None:
    """Verify gallery-dl JSON error records map to anonymous availability errors."""
    output = json.dumps(
        [
            [
                -1,
                {
                    "error": "AbortExtraction",
                    "message": "HTTP redirect to login page (https://www.instagram.com/accounts/login/)",
                },
            ]
        ]
    ).encode()
    process = FakeProcess(output + b"\n")

    async def fake_create(*_args: Any, **_kwargs: Any) -> FakeProcess:
        """Return a successful process containing a structured login error."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    runner = GalleryDlRunner(Settings())

    with pytest.raises(AppError) as exc_info:
        await runner.extract(validate_post_url("https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.code == "post_unavailable"


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
