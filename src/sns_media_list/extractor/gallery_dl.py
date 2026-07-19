"""Isolated gallery-dl subprocess runner."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import AppError
from ..url_validation import ValidatedPostUrl


def build_gallery_command(
    post_url: str,
    *,
    proxy_url: str,
    timeout_seconds: float = 45.0,
) -> list[str]:
    """Build a shell-free gallery-dl command with direct-media settings."""
    return [
        "gallery-dl",
        "--config-ignore",
        "--no-input",
        "--no-download",
        "--resolve-json",
        "--no-colors",
        "--http-timeout",
        str(timeout_seconds),
        "--retries",
        "0",
        "--whitelist",
        "instagram,twitter",
        "--proxy",
        proxy_url,
        "-o",
        "extractor.instagram.videos=merged",
        "-o",
        "extractor.instagram.previews=false",
        "-o",
        "extractor.twitter.videos=true",
        "-o",
        "extractor.twitter.previews=false",
        post_url,
    ]


def build_sanitized_environment(
    base: dict[str, str],
    *,
    home: str,
    proxy_url: str,
) -> dict[str, str]:
    """Build a minimal subprocess environment with forced proxy settings."""
    environment: dict[str, str] = {
        "PATH": base.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": base.get("LANG", "C.UTF-8"),
        "LC_ALL": base.get("LC_ALL", "C.UTF-8"),
        "HOME": home,
        "TMPDIR": home,
        "XDG_CONFIG_HOME": str(Path(home) / "config"),
        "XDG_CACHE_HOME": str(Path(home) / "cache"),
        "NO_COLOR": "1",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": "",
    }
    return environment


class GalleryDlRunner:
    """Run pinned gallery-dl extraction in an isolated subprocess."""

    def __init__(self, settings: Settings, *, proxy_url: str | None = None) -> None:
        """Store bounded runtime settings and the mandatory proxy URL."""
        self.settings = settings
        self.proxy_url = proxy_url or (
            f"http://{settings.extraction_proxy_host}:{settings.extraction_proxy_port}"
        )

    async def extract(self, post_url: ValidatedPostUrl) -> list[dict[str, object]]:
        """Extract JSON records for a validated post and map safe errors."""
        with tempfile.TemporaryDirectory(prefix="sns-gallery-") as home:
            command = build_gallery_command(
                post_url.canonical_url,
                proxy_url=self.proxy_url,
                timeout_seconds=self.settings.extraction_timeout_seconds,
            )
            environment = build_sanitized_environment(
                dict(os.environ), home=home, proxy_url=self.proxy_url
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            stdout, stderr = await self._communicate(process)
        if len(stdout) > self.settings.extraction_output_limit:
            raise AppError("extraction_failed", "The extractor output exceeded its limit.")
        if process.returncode != 0:
            raise _map_process_error(stderr)
        records = _map_json_error_records(_parse_json_records(stdout))
        return _add_post_context(records, post_url)

    async def _communicate(self, process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """Collect bounded subprocess output and terminate on timeout."""
        try:
            return await asyncio.wait_for(
                process.communicate(), self.settings.extraction_timeout_seconds
            )
        except TimeoutError as error:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise AppError("extraction_timeout", "The extraction timed out.") from error


def _parse_json_records(stdout: bytes) -> list[dict[str, object]]:
    """Parse JSON objects or arrays from extractor stdout without exposing raw data."""
    try:
        text = stdout.decode("utf-8")
        try:
            records = _records_from_value(json.loads(text))
        except json.JSONDecodeError:
            records = []
            for line in text.splitlines():
                if line.strip():
                    records.extend(_records_from_value(json.loads(line)))
        if not records:
            raise ValueError("no records")
        return records
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AppError("extraction_failed", "The extractor returned invalid output.") from error


def _records_from_value(value: Any) -> list[dict[str, object]]:
    """Convert gallery-dl objects or message tuples into metadata records."""
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        raise ValueError("record is not an object")
    if len(value) >= 2 and isinstance(value[-1], dict) and not isinstance(value[0], dict):
        if value[0] == 2:
            return []
        record = dict(value[-1])
        if value[0] == 3 and isinstance(value[1], str):
            record.setdefault("url", value[1])
        return [record]

    records: list[dict[str, object]] = []
    for item in value:
        records.extend(_records_from_value(item))
    return records


def _add_post_context(
    records: list[dict[str, object]], post_url: ValidatedPostUrl
) -> list[dict[str, object]]:
    """Add application-owned post context and safe text fields to media records."""
    contextualized: list[dict[str, object]] = []
    for record in records:
        if "error" in record:
            contextualized.append(record)
            continue
        item = dict(record)
        item.setdefault("platform", post_url.platform)
        item.setdefault("post_url", post_url.canonical_url)
        item.setdefault("post_id", post_url.post_id)
        item.setdefault("progressive", True)

        author = item.get("author")
        if isinstance(author, dict):
            name = author.get("name") or author.get("nick")
            item["author"] = name if isinstance(name, str) else None
        elif author is not None and not isinstance(author, str):
            item["author"] = None

        if not isinstance(item.get("description"), str):
            content = item.get("content")
            item["description"] = content if isinstance(content, str) else None
        contextualized.append(item)
    return contextualized


def _map_process_error(stderr: bytes) -> AppError:
    """Map private extractor diagnostics to a stable application error."""
    message = stderr.decode("utf-8", errors="replace")
    return _map_extractor_message(message)


def _map_json_error_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Map structured gallery-dl error records before normalizer processing."""
    for record in records:
        if "error" in record:
            message = f"{record.get('error', '')} {record.get('message', '')}"
            raise _map_extractor_message(message)
    return records


def _map_extractor_message(message: str) -> AppError:
    """Map extractor diagnostics to stable application errors without exposing details."""
    message = message.lower()
    if any(marker in message for marker in ("login", "private", "authentication", "auth required")):
        return AppError("post_unavailable", "This post is not available anonymously.")
    if any(marker in message for marker in ("429", "rate limit", "too many requests")):
        return AppError("upstream_rate_limited", "The source platform is rate limiting requests.")
    return AppError("extraction_failed", "The source platform could not be extracted.")
