"""Run owner-controlled live extraction smoke tests without logging source URLs."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from sns_media_list.errors import AppError
from sns_media_list.url_validation import validate_post_url


class SafeArgumentParser(argparse.ArgumentParser):
    """Parse arguments without echoing rejected caller-controlled values."""

    def error(self, message: str) -> NoReturn:
        """Exit with a fixed error instead of exposing argparse details."""
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments\n")

    def require_story_file(self) -> NoReturn:
        """Exit with fixed guidance for Story URLs supplied on the command line."""
        self.print_usage(sys.stderr)
        self.exit(
            2,
            "error: direct Story URL input is not accepted; use --instagram-story-file\n",
        )


CASES = (
    "instagram_image",
    "instagram_reel",
    "instagram_mixed",
    "x_image",
    "x_video",
    "x_gif",
)
_FORBIDDEN_KEYS = frozenset({"source_url", "request_headers", "cookies", "raw", "extractor"})
_MAX_STORY_URL_BYTES = 2048


def _iter_raw_case_values(arguments: Sequence[str]) -> Iterable[str]:
    """Yield every raw value assigned to any required smoke case flag."""
    case_options = {f"--{case.replace('_', '-')}" for case in CASES}
    for index, argument in enumerate(arguments):
        if argument in case_options:
            if index + 1 < len(arguments):
                yield arguments[index + 1]
            continue
        option, separator, value = argument.partition("=")
        if separator and option in case_options:
            yield value


def _is_story_url(value: str) -> bool:
    """Return whether an argument value validates as an exact Story URL."""
    try:
        return validate_post_url(value).kind == "story"
    except AppError:
        return False


def parse_arguments() -> argparse.Namespace:
    """Parse the local endpoint, six required posts, and optional Story file."""
    parser = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--verify-previews",
        action="store_true",
        help="request every preview twice and validate application-owned JPEG responses",
    )
    for case in CASES:
        parser.add_argument(f"--{case.replace('_', '-')}", required=True)
    parser.add_argument(
        "--instagram-story-file",
        metavar="ABSOLUTE_PATH",
        help="read one optional owner-controlled exact Story URL from a private file",
    )
    arguments = sys.argv[1:]
    if any(
        argument == "--instagram-story" or argument.startswith("--instagram-story=")
        for argument in arguments
    ):
        parser.require_story_file()
    if any(_is_story_url(value) for value in _iter_raw_case_values(arguments)):
        parser.require_story_file()
    parsed = parser.parse_args(arguments)
    if any(_is_story_url(getattr(parsed, case)) for case in CASES):
        parser.require_story_file()
    return parsed


def load_instagram_story_url(file_path: str) -> str:
    """Load and validate one exact Instagram Story URL from a private file."""
    path = Path(file_path)
    if not path.is_absolute():
        raise ValueError("the Instagram Story URL file path must be absolute")

    try:
        path_status = path.lstat()
    except OSError:
        raise ValueError("the Instagram Story URL file could not be opened") from None
    if not stat.S_ISREG(path_status.st_mode) or stat.S_ISLNK(path_status.st_mode):
        raise ValueError("the Instagram Story URL file must be a regular non-symlink file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as story_file:
            opened_status = os.fstat(story_file.fileno())
            if not stat.S_ISREG(opened_status.st_mode):
                raise ValueError("the Instagram Story URL file must be a regular non-symlink file")
            if opened_status.st_mode & 0o077:
                raise ValueError("the Instagram Story URL file must deny access to group or other")
            content = story_file.read(_MAX_STORY_URL_BYTES + 3)
    except ValueError:
        raise
    except OSError:
        raise ValueError("the Instagram Story URL file could not be opened") from None

    if len(content) > _MAX_STORY_URL_BYTES + 2:
        raise ValueError("the Instagram Story URL file content is invalid")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ValueError("the Instagram Story URL file content is invalid") from None
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise ValueError("the Instagram Story URL file must contain one non-empty line")
    story_url = lines[0]
    if len(story_url.encode("utf-8")) > _MAX_STORY_URL_BYTES:
        raise ValueError("the Instagram Story URL file content is invalid")

    try:
        target = validate_post_url(story_url)
    except AppError:
        raise ValueError(
            "the Instagram Story URL file does not contain an exact Story URL"
        ) from None
    if target.platform != "instagram" or target.kind != "story":
        raise ValueError("the Instagram Story URL file does not contain an exact Story URL")
    return target.canonical_url


def post_extraction(base_url: str, post_url: str) -> tuple[int, dict[str, Any]]:
    """Submit one URL and parse only the stable JSON response envelope."""
    request = Request(
        f"{base_url.rstrip('/')}/api/extractions",
        data=json.dumps({"url": post_url}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        try:
            payload = json.load(error)
        except (TypeError, ValueError):
            payload = {"code": "invalid_error_response"}
        return error.code, payload
    except URLError as error:
        raise RuntimeError("the local service could not be reached") from error


def walk_values(value: Any) -> Iterable[tuple[str | None, Any]]:
    """Yield object keys and values for a privacy contract audit."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)


def validate_response(status: int, payload: dict[str, Any]) -> int:
    """Validate public response privacy and return the ordered media count."""
    if status != 200:
        code = payload.get("code", "unknown")
        raise RuntimeError(f"unexpected application error {code}")
    keys_and_values = list(walk_values(payload))
    leaked_keys = {key for key, _value in keys_and_values if key in _FORBIDDEN_KEYS}
    if leaked_keys:
        raise RuntimeError("public response contains private extractor fields")
    media = payload.get("media")
    if not isinstance(media, list) or not media:
        raise RuntimeError("successful extraction did not return media")
    for item in media:
        if not isinstance(item, dict) or not isinstance(item.get("token"), str):
            raise RuntimeError("media item does not contain an opaque token")
        for field in ("download_url", "preview_url"):
            url = item.get(field)
            if url is not None and (not isinstance(url, str) or not url.startswith("/api/media/")):
                raise RuntimeError("media response contains a non-application URL")
    return len(media)


def verify_downloads(base_url: str, payload: dict[str, Any]) -> None:
    """Open each download endpoint and read only its first response byte."""
    media = payload["media"]
    for item in media:
        if not isinstance(item, dict):
            raise RuntimeError("media item is malformed")
        download_url = item.get("download_url")
        if not isinstance(download_url, str):
            raise RuntimeError("media item is missing its download URL")
        request = Request(urljoin(f"{base_url.rstrip('/')}/", download_url.lstrip("/")))
        with urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise RuntimeError("download endpoint did not return success")
            if response.headers.get("X-Content-Type-Options") != "nosniff":
                raise RuntimeError("download endpoint omitted nosniff")
            if not response.read(1):
                raise RuntimeError("download endpoint returned an empty body")


def verify_previews(base_url: str, payload: dict[str, Any]) -> None:
    """Open each preview endpoint twice and validate safe, non-empty JPEG responses."""
    media = payload["media"]
    for item in media:
        if not isinstance(item, dict):
            raise RuntimeError("media item is malformed")
        preview_url = item.get("preview_url")
        if not isinstance(preview_url, str):
            raise RuntimeError("media item is missing its preview URL")
        for _attempt in range(2):
            request = Request(urljoin(f"{base_url.rstrip('/')}/", preview_url.lstrip("/")))
            with urlopen(request, timeout=120) as response:
                if response.status != 200:
                    raise RuntimeError("preview endpoint did not return success")
                if response.headers.get("Content-Type", "").split(";", 1)[0] not in {
                    "image/jpeg",
                    "image/png",
                    "image/gif",
                    "image/webp",
                }:
                    raise RuntimeError("preview endpoint did not return a supported raster image")
                if response.headers.get("Cache-Control") != "private, no-store":
                    raise RuntimeError("preview endpoint omitted no-store cache control")
                if not response.read():
                    raise RuntimeError("preview endpoint returned an empty body")


def main() -> int:
    """Run required and optional live smoke cases with non-sensitive output."""
    arguments = parse_arguments()
    cases = [(case, getattr(arguments, case)) for case in CASES]
    if arguments.instagram_story_file is not None:
        story_url = load_instagram_story_url(arguments.instagram_story_file)
        cases.append(("instagram_story", story_url))
    for case, post_url in cases:
        status, payload = post_extraction(arguments.base_url, post_url)
        count = validate_response(status, payload)
        if arguments.verify_previews:
            verify_previews(arguments.base_url, payload)
        verify_downloads(arguments.base_url, payload)
        print(f"{case}: status={status} media_items={count} outcome=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
