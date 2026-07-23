"""Isolated gallery-dl subprocess runner."""

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import AppError
from ..url_validation import TargetKind, ValidatedExtractionTarget

_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_HTTP_STATUS_PATTERN = re.compile(
    r"""
    \b(?:
        (?:HTTP(?:/[0-9](?:\.[0-9])?)?|status(?:\s+code)?)
        (?:\s*[:=]\s*|\s+)(?P<prefixed>[1-5][0-9]{2})
        |(?P<reasoned>
            401\s+Unauthorized
            |403\s+Forbidden
            |404\s+Not\s+Found
            |429\s+Too\s+Many\s+Requests
        )
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_RATE_LIMIT_PHRASE_PATTERN = re.compile(
    r"\btoo\s+many\s+requests\b|\brate(?:\s+|-)limit(?:s|ed|ing)?\b",
    re.IGNORECASE,
)
_EXPLICIT_SESSION_FAILURE_PATTERN = re.compile(
    r"""
    \b(?:
        (?:invalid|expired|missing)\s+(?:cookie|session)
        |(?:cookie|session)(?:\s+is)?\s+(?:invalid|expired|missing)
        |authentication(?:\s+has)?\s+(?:failed|failure)
        |login(?:\s+has)?\s+failed
        |login\s+page
        |http\s+redirect\s+to\s+(?:a\s+)?(?:challenge|consent)\s+page
        |checkpoint\s+challenge
        |(?:checkpoint|challenge|consent)(?:\s+is)?\s+required
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BROAD_AUTHENTICATION_PATTERN = re.compile(
    r"\b(?:authentication|auth\s+required|authrequired|authenticated\s+cookies\s+needed|"
    r"login\s+required)\b",
    re.IGNORECASE,
)
_AUTHENTICATION_FAILURE_PATTERN = re.compile(
    r"\b(?:authenticationerror|invalid\s+login\s+credentials)\b",
    re.IGNORECASE,
)
_STORY_UNAVAILABLE_PATTERN = re.compile(
    r"""
    \b(?:
        authrequired
        |auth(?:entication)?\s+required
        |authenticated\s+cookies\s+needed
        |credentials\s+required
        |insufficient\s+privileges
        |login\s+(?:page|required)
        |http\s+redirect\s+to\s+(?:a\s+)?login\s+page
        |private\s+(?:story|account|content|post)
        |(?:story|account|content|post)\s+is\s+private
        |notfounderror
        |requested\s+story(?:\s+[0-9]+)?\s+could\s+not\s+be\s+found
        |story\s+could\s+not\s+be\s+found
        |story\s+(?:was\s+)?not\s+found
        |story\s+does\s+not\s+exist
        |story\s+(?:has\s+|is\s+)?expired
        |expired\s+story
        |story\s+(?:is\s+)?(?:unavailable|not\s+available)
        |story\s+is\s+no\s+longer\s+available
        |story\s+(?:has\s+been|was)\s+deleted
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_POST_UNAVAILABLE_PATTERN = re.compile(
    r"""
    \b(?:
        authrequired
        |auth(?:entication)?\s+required
        |authenticated\s+cookies\s+needed
        |login\s+(?:page|required)
        |http\s+redirect\s+to\s+(?:a\s+)?login\s+page
        |private\s+(?:posts?|account|content)
        |(?:posts?|account|content)\s+(?:are|is)\s+private
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_AUTHENTICATION_ERROR_TYPE = "authenticationerror"
_AUTH_REQUIRED_ERROR_TYPE = "authrequired"
_HTTP_ERROR_TYPE = "httperror"
_NOT_FOUND_ERROR_TYPE = "notfounderror"
_KNOWN_ERROR_TYPES = frozenset(
    {
        _AUTHENTICATION_ERROR_TYPE,
        _AUTH_REQUIRED_ERROR_TYPE,
        _HTTP_ERROR_TYPE,
        _NOT_FOUND_ERROR_TYPE,
    }
)


@dataclass(frozen=True, slots=True)
class _ParsedGalleryOutput:
    """Separate pinned DataJob media, errors, and empty-result context."""

    media_records: tuple[dict[str, object], ...]
    error_records: tuple[dict[str, object], ...]
    literal_empty: bool
    saw_non_media: bool


def build_gallery_command(
    target: str,
    *,
    proxy_url: str,
    timeout_seconds: float = 45.0,
    cookie_file: str | None = None,
) -> list[str]:
    """Build a shell-free gallery-dl command with direct-media settings."""
    command = [
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
    ]
    if cookie_file is not None:
        category = "instagram" if "instagram.com/" in target else "twitter"
        command.extend(
            [
                "-o",
                f"extractor.{category}.cookies={cookie_file}",
                "-o",
                f"extractor.{category}.cookies-update=false",
            ]
        )
    command.append(target)
    return command


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

    async def extract(self, target: ValidatedExtractionTarget) -> list[dict[str, object]]:
        """Extract JSON records for a validated target and map safe errors."""
        with tempfile.TemporaryDirectory(prefix="sns-gallery-") as home:
            cookie_file = _cookie_file_for_platform(self.settings, target.platform)
            command = build_gallery_command(
                target.canonical_url,
                proxy_url=self.proxy_url,
                timeout_seconds=self.settings.extraction_timeout_seconds,
                cookie_file=cookie_file,
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
            raise _map_process_error(
                stderr,
                target_kind=target.kind,
                authenticated=cookie_file is not None,
            )
        parsed = _parse_json_records(stdout)
        _map_json_error_records(
            list(parsed.error_records),
            target_kind=target.kind,
            authenticated=cookie_file is not None,
        )
        records = list(parsed.media_records)
        if not records:
            if parsed.literal_empty:
                if target.kind == "story":
                    raise AppError("story_unavailable", "This Story is unavailable.")
                raise AppError("extraction_failed", "The extractor returned invalid output.")
            if parsed.saw_non_media:
                raise AppError("no_media", "No directly downloadable media was found.")
            raise AppError("extraction_failed", "The extractor returned invalid output.")
        return _add_post_context(records, target)

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


def _parse_json_records(stdout: bytes) -> _ParsedGalleryOutput:
    """Parse only the pinned non-JSONL DataJob top-level event array."""
    try:
        text = stdout.decode("utf-8")
        if not text.strip():
            raise ValueError("empty output")
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("DataJob output is not an array")
        if not value:
            return _ParsedGalleryOutput((), (), True, False)

        media_records: list[dict[str, object]] = []
        error_records: list[dict[str, object]] = []
        saw_non_media = False
        for item in value:
            record, is_error, is_non_media = _record_from_message_tuple(item)
            saw_non_media = saw_non_media or is_non_media
            if record is not None:
                if is_error:
                    error_records.append(record)
                else:
                    media_records.append(record)
        return _ParsedGalleryOutput(
            tuple(media_records), tuple(error_records), False, saw_non_media
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AppError("extraction_failed", "The extractor returned invalid output.") from error


def _record_from_message_tuple(
    value: Any,
) -> tuple[dict[str, object] | None, bool, bool]:
    """Validate one DataJob event and identify error or non-media context."""
    if not isinstance(value, list) or not value:
        raise ValueError("DataJob item is not an event tuple")
    code = value[0]
    if type(code) is not int:
        raise ValueError("message code is not an integer")
    if code == -1:
        if len(value) != 2 or not isinstance(value[1], dict):
            raise ValueError("invalid error event")
        record = dict(value[1])
        if not isinstance(record.get("error"), str) or not isinstance(record.get("message"), str):
            raise ValueError("invalid error metadata")
        return record, True, False
    if code == 2:
        if len(value) != 2 or not isinstance(value[1], dict):
            raise ValueError("invalid directory event")
        return None, False, True
    if code in (3, 6):
        if len(value) != 3 or not isinstance(value[1], str) or not isinstance(value[2], dict):
            raise ValueError("invalid URL or queue event")
        if code == 6:
            return None, False, True
        record = dict(value[2])
        record.setdefault("url", value[1])
        return record, False, False
    raise ValueError("unknown message code")


def _add_post_context(
    records: list[dict[str, object]], target: ValidatedExtractionTarget
) -> list[dict[str, object]]:
    """Add application-owned target context and safe text fields to media records."""
    contextualized: list[dict[str, object]] = []
    for record in records:
        if "error" in record:
            contextualized.append(record)
            continue
        item = dict(record)
        if target.kind == "story":
            item["platform"] = target.platform
            item["post_url"] = target.canonical_url
            item["post_id"] = target.target_id
        else:
            item.setdefault("platform", target.platform)
            item.setdefault("post_url", target.canonical_url)
            item.setdefault("post_id", target.target_id)
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


def _map_process_error(
    stderr: bytes,
    *,
    target_kind: TargetKind,
    authenticated: bool,
) -> AppError:
    """Map private extractor diagnostics to a stable application error."""
    message = stderr.decode("utf-8", errors="replace")
    return _map_extractor_error(
        error_type=None,
        message=message,
        target_kind=target_kind,
        authenticated=authenticated,
    )


def _map_json_error_records(
    records: list[dict[str, object]],
    *,
    target_kind: TargetKind,
    authenticated: bool,
) -> list[dict[str, object]]:
    """Map structured gallery-dl error records before normalizer processing."""
    for record in records:
        if "error" in record:
            raw_error_type = record.get("error")
            raw_message = record.get("message")
            raise _map_extractor_error(
                error_type=raw_error_type if isinstance(raw_error_type, str) else None,
                message=raw_message if isinstance(raw_message, str) else "",
                target_kind=target_kind,
                authenticated=authenticated,
            )
    return records


def _map_extractor_error(
    error_type: str | None,
    message: str,
    *,
    target_kind: TargetKind,
    authenticated: bool,
) -> AppError:
    """Classify separate extractor error types and messages into stable errors."""
    normalized_type = error_type.strip().casefold() if error_type is not None else None
    diagnostic = _remove_http_urls(message)
    statuses = _extract_http_statuses(diagnostic)
    if 429 in statuses:
        return AppError("upstream_rate_limited", "The source platform is rate limiting requests.")
    if authenticated and normalized_type == _AUTHENTICATION_ERROR_TYPE:
        return AppError(
            "platform_authentication_failed",
            "The configured platform session is unavailable. Contact the service operator.",
        )
    if target_kind == "story" and normalized_type == _NOT_FOUND_ERROR_TYPE:
        return AppError("story_unavailable", "This Story is unavailable.")
    if normalized_type == _AUTH_REQUIRED_ERROR_TYPE:
        if target_kind == "story":
            return AppError("story_unavailable", "This Story is unavailable.")
        if authenticated:
            return AppError(
                "platform_authentication_failed",
                "The configured platform session is unavailable. Contact the service operator.",
            )
        return AppError("post_unavailable", "This post is not available anonymously.")
    if not authenticated and normalized_type == _AUTHENTICATION_ERROR_TYPE:
        if target_kind == "story":
            return AppError("story_unavailable", "This Story is unavailable.")
        return AppError("post_unavailable", "This post is not available anonymously.")
    if (
        target_kind == "story"
        and normalized_type == _HTTP_ERROR_TYPE
        and statuses.intersection({401, 403, 404})
    ):
        return AppError("story_unavailable", "This Story is unavailable.")
    if normalized_type not in _KNOWN_ERROR_TYPES and _RATE_LIMIT_PHRASE_PATTERN.search(diagnostic):
        return AppError("upstream_rate_limited", "The source platform is rate limiting requests.")
    explicit_authentication_failure = (
        _EXPLICIT_SESSION_FAILURE_PATTERN.search(diagnostic) is not None
        or _AUTHENTICATION_FAILURE_PATTERN.search(diagnostic) is not None
    )
    broad_post_authentication_failure = target_kind == "post" and (
        _BROAD_AUTHENTICATION_PATTERN.search(diagnostic) is not None
    )
    if authenticated and (explicit_authentication_failure or broad_post_authentication_failure):
        return AppError(
            "platform_authentication_failed",
            "The configured platform session is unavailable. Contact the service operator.",
        )
    if target_kind == "story" and statuses.intersection({401, 403, 404}):
        return AppError("story_unavailable", "This Story is unavailable.")
    if not authenticated and _AUTHENTICATION_FAILURE_PATTERN.search(diagnostic) is not None:
        if target_kind == "story":
            return AppError("story_unavailable", "This Story is unavailable.")
        return AppError("post_unavailable", "This post is not available anonymously.")
    if target_kind == "story" and (_STORY_UNAVAILABLE_PATTERN.search(diagnostic) is not None):
        return AppError("story_unavailable", "This Story is unavailable.")
    if _POST_UNAVAILABLE_PATTERN.search(diagnostic) is not None:
        return AppError("post_unavailable", "This post is not available anonymously.")
    return AppError("extraction_failed", "The source platform could not be extracted.")


def _remove_http_urls(message: str) -> str:
    """Remove HTTP and HTTPS URLs before diagnostic phrase classification."""
    return _URL_PATTERN.sub("", message)


def _extract_http_statuses(message: str) -> set[int]:
    """Return HTTP statuses with an HTTP prefix, status prefix, or reason phrase."""
    statuses: set[int] = set()
    for match in _HTTP_STATUS_PATTERN.finditer(message):
        prefixed = match.group("prefixed")
        if prefixed is not None:
            statuses.add(int(prefixed))
        else:
            reasoned = match.group("reasoned")
            if reasoned is not None:
                statuses.add(int(reasoned.split(maxsplit=1)[0]))
    return statuses


def _cookie_file_for_platform(settings: Settings, platform: str) -> str | None:
    """Return only the configured Cookie path for the selected platform."""
    if platform == "instagram":
        return settings.instagram_cookie_file
    if platform == "x":
        return settings.x_cookie_file
    return None
