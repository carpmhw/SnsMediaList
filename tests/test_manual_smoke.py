"""Tests for the owner-controlled manual smoke script."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import manual_smoke

_STORY_URL = "https://www.instagram.com/stories/example.user/1234567890/"
_DIRECT_STORY_SENTINEL = "https://sentinel.invalid/stories/private/1234567890/"
_REQUIRED_ARGUMENTS: tuple[str, ...] = (
    "--instagram-image",
    "https://www.instagram.com/p/OWNER_IMAGE/",
    "--instagram-reel",
    "https://www.instagram.com/reel/OWNER_REEL/",
    "--instagram-mixed",
    "https://www.instagram.com/p/OWNER_MIXED/",
    "--x-image",
    "https://x.com/owner/status/1001",
    "--x-video",
    "https://x.com/owner/status/1002",
    "--x-gif",
    "https://x.com/owner/status/1003",
)


def _write_private_file(path: Path, content: bytes) -> None:
    """Write one owner-only smoke input file without exposing its contents."""
    path.write_bytes(content)
    path.chmod(0o600)


def test_load_instagram_story_url_accepts_private_absolute_file(tmp_path: Path) -> None:
    """Load one exact Instagram Story URL from a private absolute file."""
    story_file = tmp_path / "story-url"
    _write_private_file(story_file, f"{_STORY_URL}\n".encode())

    assert manual_smoke.load_instagram_story_url(str(story_file)) == _STORY_URL


@pytest.mark.parametrize("invalid_path", ["story-url", "nested/story-url"])
def test_load_instagram_story_url_rejects_relative_path(invalid_path: str) -> None:
    """Reject Story input paths that are not absolute."""
    with pytest.raises(ValueError, match="absolute"):
        manual_smoke.load_instagram_story_url(invalid_path)


def test_load_instagram_story_url_rejects_symlink(tmp_path: Path) -> None:
    """Reject a symlink even when its target is a private regular file."""
    target = tmp_path / "target"
    link = tmp_path / "story-url"
    _write_private_file(target, _STORY_URL.encode())
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        manual_smoke.load_instagram_story_url(str(link))


def test_load_instagram_story_url_rejects_non_regular_file(tmp_path: Path) -> None:
    """Reject directories and other non-regular filesystem objects."""
    directory = tmp_path / "story-url"
    directory.mkdir()

    with pytest.raises(ValueError, match="regular non-symlink"):
        manual_smoke.load_instagram_story_url(str(directory))


@pytest.mark.parametrize("mode", [0o604, 0o620, 0o644])
def test_load_instagram_story_url_rejects_group_or_other_permissions(
    tmp_path: Path, mode: int
) -> None:
    """Reject input files readable or writable by group or other users."""
    story_file = tmp_path / "story-url"
    _write_private_file(story_file, _STORY_URL.encode())
    story_file.chmod(mode)

    with pytest.raises(ValueError, match="group or other"):
        manual_smoke.load_instagram_story_url(str(story_file))


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"not-utf8-\xff",
        f"{_STORY_URL}\n{_STORY_URL}\n".encode(),
        f" {_STORY_URL}\n".encode(),
        ("x" * 2049).encode(),
    ],
)
def test_load_instagram_story_url_rejects_invalid_content(tmp_path: Path, content: bytes) -> None:
    """Reject non-UTF-8, empty, multiline, padded, or excessive input."""
    story_file = tmp_path / "story-url"
    _write_private_file(story_file, content)

    with pytest.raises(ValueError) as error:
        manual_smoke.load_instagram_story_url(str(story_file))

    assert _STORY_URL not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/OWNER_CONTROLLED/",
        "https://x.com/owner/status/1234567890",
        "https://www.instagram.com/stories/example.user/",
    ],
)
def test_load_instagram_story_url_rejects_non_story_target(tmp_path: Path, url: str) -> None:
    """Reject supported posts and imprecise Story targets without echoing input."""
    story_file = tmp_path / "story-url"
    _write_private_file(story_file, url.encode())

    with pytest.raises(ValueError) as error:
        manual_smoke.load_instagram_story_url(str(story_file))

    assert url not in str(error.value)


def test_load_instagram_story_url_rejects_missing_path_without_os_detail(tmp_path: Path) -> None:
    """Reject an absent file without forwarding operating-system path details."""
    missing = tmp_path / "missing-story-url"

    with pytest.raises(ValueError) as error:
        manual_smoke.load_instagram_story_url(str(missing))

    assert os.fspath(missing) not in str(error.value)


def test_argument_help_offers_only_story_file_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advertise the optional file flag without a direct Story URL flag."""
    monkeypatch.setattr(sys, "argv", ["manual_smoke.py", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--instagram-story-file" in help_text
    assert re.search(r"--instagram-story(?:[ =]|$)", help_text) is None


@pytest.mark.parametrize(
    "direct_arguments",
    [
        ("--instagram-story", _DIRECT_STORY_SENTINEL),
        (f"--instagram-story={_DIRECT_STORY_SENTINEL}",),
    ],
)
def test_parse_arguments_rejects_direct_story_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    direct_arguments: tuple[str, ...],
) -> None:
    """Reject direct Story input with a fixed file-based usage hint."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["manual_smoke.py", *_REQUIRED_ARGUMENTS, *direct_arguments],
    )

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "direct Story URL input is not accepted; use --instagram-story-file" in output.err
    assert _DIRECT_STORY_SENTINEL not in output.out
    assert _DIRECT_STORY_SENTINEL not in output.err


@pytest.mark.parametrize(
    "invalid_argument",
    [
        f"--instagram-sto={_DIRECT_STORY_SENTINEL}",
        f"--other={_DIRECT_STORY_SENTINEL}",
    ],
)
def test_parse_arguments_hides_all_invalid_argument_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_argument: str,
) -> None:
    """Hide values for Story-like and unrelated invalid arguments."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["manual_smoke.py", *_REQUIRED_ARGUMENTS, invalid_argument],
    )

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "invalid arguments" in output.err
    assert _DIRECT_STORY_SENTINEL not in output.out
    assert _DIRECT_STORY_SENTINEL not in output.err


@pytest.mark.parametrize(
    "file_arguments",
    [
        ("--instagram-story-file", "/private/story-url"),
        ("--instagram-story-file=/private/story-url",),
    ],
)
def test_parse_arguments_accepts_story_file_flag(
    monkeypatch: pytest.MonkeyPatch, file_arguments: tuple[str, ...]
) -> None:
    """Accept either explicit form of the optional Story file flag."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["manual_smoke.py", *_REQUIRED_ARGUMENTS, *file_arguments],
    )

    arguments = manual_smoke.parse_arguments()

    assert arguments.instagram_story_file == "/private/story-url"


@pytest.mark.parametrize("case", manual_smoke.CASES)
def test_parse_arguments_rejects_story_url_in_required_case(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Reject exact Story URLs passed through any required post case flag."""
    required_arguments = list(_REQUIRED_ARGUMENTS)
    option = f"--{case.replace('_', '-')}"
    required_arguments[required_arguments.index(option) + 1] = _STORY_URL
    monkeypatch.setattr(sys, "argv", ["manual_smoke.py", *required_arguments])

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "use --instagram-story-file" in output.err
    assert _STORY_URL not in output.out
    assert _STORY_URL not in output.err


def test_parse_arguments_rejects_story_hidden_by_duplicate_case(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a Story even when a later duplicate case supplies a post URL."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["manual_smoke.py", "--instagram-image", _STORY_URL, *_REQUIRED_ARGUMENTS],
    )

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "use --instagram-story-file" in output.err
    assert _STORY_URL not in output.out
    assert _STORY_URL not in output.err


def test_parse_arguments_rejects_story_before_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a separated Story case value before argparse handles help."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["manual_smoke.py", "--instagram-image", _STORY_URL, "--help"],
    )

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "use --instagram-story-file" in output.err
    assert _STORY_URL not in output.out
    assert _STORY_URL not in output.err


@pytest.mark.parametrize("case", manual_smoke.CASES)
def test_parse_arguments_rejects_equals_story_before_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Reject equals-form Story values for every case before handling help."""
    option = f"--{case.replace('_', '-')}={_STORY_URL}"
    monkeypatch.setattr(sys, "argv", ["manual_smoke.py", option, "--help"])

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "use --instagram-story-file" in output.err
    assert _STORY_URL not in output.out
    assert _STORY_URL not in output.err


def test_parse_arguments_keeps_missing_case_value_error_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Leave missing required case values to the fixed argparse error."""
    monkeypatch.setattr(sys, "argv", ["manual_smoke.py", "--instagram-image"])

    with pytest.raises(SystemExit) as exit_info:
        manual_smoke.parse_arguments()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "invalid arguments" in output.err
    assert output.out == ""


def test_parse_arguments_leaves_other_invalid_urls_for_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave non-Story URL validation to the service extraction request."""
    required_arguments = list(_REQUIRED_ARGUMENTS)
    required_arguments[1] = "not-a-url"
    monkeypatch.setattr(sys, "argv", ["manual_smoke.py", *required_arguments])

    arguments = manual_smoke.parse_arguments()

    assert arguments.instagram_image == "not-a-url"


def test_main_runs_optional_story_without_sensitive_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run the optional Story through the standard extraction and media checks."""
    required_urls = {case: f"https://owner.invalid/{case}" for case in manual_smoke.CASES}
    arguments = SimpleNamespace(
        base_url="http://127.0.0.1:8000",
        verify_previews=True,
        instagram_story_file="/private/story-url",
        **required_urls,
    )
    submitted_urls: list[str] = []
    preview_payloads: list[dict[str, Any]] = []
    download_payloads: list[dict[str, Any]] = []
    secret_token = "opaque-secret-token"
    payload = {
        "media": [
            {
                "token": secret_token,
                "preview_url": f"/api/media/{secret_token}/preview",
                "download_url": f"/api/media/{secret_token}/download",
            }
        ]
    }

    def fake_post_extraction(_base_url: str, post_url: str) -> tuple[int, dict[str, Any]]:
        """Record submitted URLs while returning a stable application response."""
        submitted_urls.append(post_url)
        return 200, payload

    def fake_verify_previews(_base_url: str, response: dict[str, Any]) -> None:
        """Record that preview checks received each extraction response."""
        preview_payloads.append(response)

    def fake_verify_downloads(_base_url: str, response: dict[str, Any]) -> None:
        """Record that download checks received each extraction response."""
        download_payloads.append(response)

    monkeypatch.setattr(manual_smoke, "parse_arguments", lambda: arguments)
    monkeypatch.setattr(manual_smoke, "load_instagram_story_url", lambda _path: _STORY_URL)
    monkeypatch.setattr(manual_smoke, "post_extraction", fake_post_extraction)
    monkeypatch.setattr(manual_smoke, "verify_previews", fake_verify_previews)
    monkeypatch.setattr(manual_smoke, "verify_downloads", fake_verify_downloads)

    assert manual_smoke.main() == 0

    output = capsys.readouterr().out
    assert submitted_urls == [*required_urls.values(), _STORY_URL]
    assert len(preview_payloads) == 7
    assert len(download_payloads) == 7
    assert output.splitlines()[-1] == "instagram_story: status=200 media_items=1 outcome=ok"
    assert all(
        re.fullmatch(r"[a-z_]+: status=\d+ media_items=\d+ outcome=ok", line)
        for line in output.splitlines()
    )
    assert _STORY_URL not in output
    assert secret_token not in output


def test_main_without_story_file_runs_only_six_required_cases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the existing six-case workflow unchanged when no Story file is given."""
    required_urls = {case: f"https://owner.invalid/{case}" for case in manual_smoke.CASES}
    arguments = SimpleNamespace(
        base_url="http://127.0.0.1:8000",
        verify_previews=False,
        instagram_story_file=None,
        **required_urls,
    )
    submitted_urls: list[str] = []
    payload = {
        "media": [
            {
                "token": "opaque-token",
                "preview_url": "/api/media/opaque-token/preview",
                "download_url": "/api/media/opaque-token/download",
            }
        ]
    }

    def fake_post_extraction(_base_url: str, post_url: str) -> tuple[int, dict[str, Any]]:
        """Record required-case submissions without performing network access."""
        submitted_urls.append(post_url)
        return 200, payload

    def reject_story_load(_path: str) -> str:
        """Fail if the absent optional Story input is accessed."""
        raise AssertionError("Story loader must not run without the optional file")

    monkeypatch.setattr(manual_smoke, "parse_arguments", lambda: arguments)
    monkeypatch.setattr(manual_smoke, "load_instagram_story_url", reject_story_load)
    monkeypatch.setattr(manual_smoke, "post_extraction", fake_post_extraction)
    monkeypatch.setattr(manual_smoke, "verify_downloads", lambda _base_url, _payload: None)

    assert manual_smoke.main() == 0

    output = capsys.readouterr().out
    assert submitted_urls == list(required_urls.values())
    assert len(output.splitlines()) == 6
    assert "instagram_story" not in output
