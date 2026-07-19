"""Run owner-controlled live extraction smoke tests without logging source URLs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

CASES = (
    "instagram_image",
    "instagram_reel",
    "instagram_mixed",
    "x_image",
    "x_video",
    "x_gif",
)
_FORBIDDEN_KEYS = frozenset({"source_url", "request_headers", "cookies", "raw", "extractor"})


def parse_arguments() -> argparse.Namespace:
    """Parse the local endpoint and six owner-controlled post URLs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    for case in CASES:
        parser.add_argument(f"--{case.replace('_', '-')}", required=True)
    return parser.parse_args()


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


def main() -> int:
    """Run all six live smoke cases and print non-sensitive outcomes."""
    arguments = parse_arguments()
    for case in CASES:
        status, payload = post_extraction(arguments.base_url, getattr(arguments, case))
        count = validate_response(status, payload)
        verify_downloads(arguments.base_url, payload)
        print(f"{case}: status={status} media_items={count} outcome=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
