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
from ..errors import (
    AppError,
    ExtractorDiagnosticSource,
    ExtractorErrorType,
    normalize_extractor_diagnostics,
)
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
            |500\s+Internal\s+Server\s+Error
            |502\s+Bad\s+Gateway
            |503\s+Service\s+Unavailable
            |504\s+Gateway\s+Timeout
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
_STORY_AUTHENTICATION_PATTERN = re.compile(
    r"""
    \b(?:
        authrequired
        |auth(?:entication)?\s+required
        |authenticated\s+cookies\s+needed
        |credentials\s+required
        |login\s+(?:page|required)
        |http\s+redirect\s+to\s+(?:a\s+)?login\s+page
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_STORY_UNAVAILABLE_PATTERN = re.compile(
    r"""
    \b(?:
        insufficient\s+privileges
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
_EXTRACTOR_DIAGNOSTIC_ERROR_TYPES: dict[str, ExtractorErrorType] = {
    "authrequired": "auth_required",
    "authenticationerror": "authentication_error",
    "authorizationerror": "authorization_error",
    "notfounderror": "not_found",
    "httperror": "http_error",
    "challengeerror": "challenge_error",
    "extractionerror": "extraction_error",
    "noextractorerror": "no_extractor",
}
_EXTRACTION_READ_CHUNK = 64 * 1024
_EXTRACTION_STDERR_LIMIT = 64 * 1024
_EXTRACTION_CLEANUP_TIMEOUT_SECONDS = 2.0


class _ExtractionOutputLimitExceeded(Exception):
    """表示 extractor stdout 已超過允許保留的大小。"""


class _ExtractionOperationTimeout(Exception):
    """表示 extractor 自身的 bounded process 等待已超時。"""


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
        """擷取驗證過的目標並將錯誤映射為安全的 application error。"""
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
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as error:
                raise AppError(
                    "extraction_failed",
                    "The extractor process could not be started.",
                    failure_stage="extractor_start",
                ) from error
            stdout, stderr = await self._communicate(process)
        if len(stdout) > self.settings.extraction_output_limit:
            raise AppError(
                "extraction_failed",
                "The extractor output exceeded its limit.",
                failure_stage="extractor_output_limit",
            )
        if process.returncode != 0:
            raise _map_process_error(
                stderr,
                exit_code=process.returncode,
                target_kind=target.kind,
                authenticated=cookie_file is not None,
            )
        try:
            parsed = _parse_json_records(stdout)
        except AppError as error:
            error.extractor_diagnostics = normalize_extractor_diagnostics(extractor_exit_code=0)
            raise
        _map_json_error_records(
            list(parsed.error_records),
            target_kind=target.kind,
            authenticated=cookie_file is not None,
        )
        records = list(parsed.media_records)
        if not records:
            if parsed.literal_empty:
                if target.kind == "story":
                    raise AppError(
                        "story_unavailable",
                        "This Story is unavailable.",
                        failure_stage="extractor_empty_output",
                        extractor_diagnostics=normalize_extractor_diagnostics(
                            extractor_exit_code=0
                        ),
                    )
                raise AppError(
                    "extraction_failed",
                    "The extractor returned invalid output.",
                    failure_stage="extractor_empty_output",
                    extractor_diagnostics=normalize_extractor_diagnostics(extractor_exit_code=0),
                )
            if parsed.saw_non_media:
                raise AppError(
                    "no_media",
                    "No directly downloadable media was found.",
                    failure_stage="extractor_no_media",
                    extractor_diagnostics=normalize_extractor_diagnostics(extractor_exit_code=0),
                )
            raise AppError(
                "extraction_failed",
                "The extractor returned invalid output.",
                failure_stage="extractor_invalid_output",
                extractor_diagnostics=normalize_extractor_diagnostics(extractor_exit_code=0),
            )
        return _add_post_context(records, target)

    async def _communicate(self, process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """以 bounded pipe readers concurrently drain subprocess output。"""
        stdout = process.stdout
        stderr = process.stderr
        if not isinstance(stdout, asyncio.StreamReader) or not isinstance(
            stderr, asyncio.StreamReader
        ):
            return await self._communicate_test_double(process)

        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(
                _read_extraction_stream(
                    stdout,
                    limit=self.settings.extraction_output_limit,
                    reject_over_limit=True,
                )
            ),
            asyncio.create_task(
                _read_extraction_stream(
                    stderr,
                    limit=_EXTRACTION_STDERR_LIMIT,
                    reject_over_limit=False,
                )
            ),
            asyncio.create_task(process.wait()),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.settings.extraction_timeout_seconds,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            if pending:
                for task in done:
                    if task.cancelled():
                        raise asyncio.CancelledError
                    error = task.exception()
                    if error is not None:
                        raise error
                raise _ExtractionOperationTimeout
            stdout_bytes, stderr_bytes, _returncode = await asyncio.gather(*tasks)
            return stdout_bytes, stderr_bytes
        except _ExtractionOutputLimitExceeded as error:
            await _stop_extraction_process(process, tasks[2])
            raise AppError(
                "extraction_failed",
                "The extractor output exceeded its limit.",
                failure_stage="extractor_output_limit",
            ) from error
        except _ExtractionOperationTimeout as error:
            await _stop_extraction_process(process, tasks[2])
            raise AppError(
                "extraction_timeout",
                "The extraction timed out.",
                failure_stage="extractor_timeout",
            ) from error
        except OSError as error:
            await _stop_extraction_process(process, tasks[2])
            raise AppError(
                "extraction_failed",
                "The extractor output could not be read.",
                failure_stage="extractor_io",
            ) from error
        except BaseException:
            await _stop_extraction_process(process, tasks[2])
            raise
        finally:
            await _cleanup_extraction_tasks(tasks)

    async def _communicate_test_double(self, process: Any) -> tuple[bytes, bytes]:
        """保留沒有 asyncio pipe 的既有 test double communicate fallback。"""
        try:
            return await asyncio.wait_for(
                process.communicate(), self.settings.extraction_timeout_seconds
            )
        except TimeoutError as error:
            await _stop_extraction_process(process)
            raise AppError(
                "extraction_timeout",
                "The extraction timed out.",
                failure_stage="extractor_timeout",
            ) from error
        except OSError as error:
            await _stop_extraction_process(process)
            raise AppError(
                "extraction_failed",
                "The extractor output could not be read.",
                failure_stage="extractor_io",
            ) from error


async def _read_extraction_stream(
    stream: asyncio.StreamReader,
    *,
    limit: int,
    reject_over_limit: bool,
) -> bytes:
    """以固定 chunk drain stream，僅在 stdout 路徑對超限立即失敗。"""
    output = bytearray()
    while True:
        if reject_over_limit:
            read_size = min(_EXTRACTION_READ_CHUNK, limit - len(output) + 1)
        else:
            read_size = _EXTRACTION_READ_CHUNK
        chunk = await stream.read(read_size)
        if not chunk:
            return bytes(output)
        if reject_over_limit and len(output) + len(chunk) > limit:
            raise _ExtractionOutputLimitExceeded
        if len(output) < limit:
            output.extend(chunk[: limit - len(output)])


async def _await_extraction_process_exit(
    process: Any,
    wait_task: asyncio.Task[Any] | None,
    timeout: float,
) -> bool:
    """在不重複建立或取消 wait task 的前提下 bounded 等待 process 結束。"""
    del process
    if wait_task is None:
        return False
    if wait_task.done():
        try:
            wait_task.result()
        except BaseException:
            return False
        return True
    try:
        done, _pending = await asyncio.wait({wait_task}, timeout=timeout)
    except asyncio.CancelledError:
        _observe_extraction_wait_task(wait_task)
        raise
    if wait_task not in done:
        return False
    try:
        wait_task.result()
    except BaseException:
        return False
    return True


async def _stop_extraction_process(
    process: Any,
    wait_task: asyncio.Task[Any] | None = None,
) -> None:
    """終止 extractor 並在 terminate 無效時 fallback 到 kill，保留 primary error。"""
    cancellation_received = False
    try:
        process.terminate()
    except OSError:
        pass
    if wait_task is None:
        try:
            wait_task = asyncio.create_task(process.wait())
        except BaseException:
            wait_task = None
    try:
        exited = await _await_extraction_process_exit(
            process, wait_task, _EXTRACTION_CLEANUP_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        cancellation_received = True
        exited = False
    except BaseException:
        exited = False
    if not exited:
        try:
            process.kill()
        except OSError:
            pass
        try:
            await _await_extraction_process_exit(
                process, wait_task, _EXTRACTION_CLEANUP_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            cancellation_received = True
        except BaseException:
            pass
    if wait_task is not None:
        _observe_extraction_wait_task(wait_task)
    if cancellation_received:
        raise asyncio.CancelledError


def _observe_extraction_wait_task(task: asyncio.Task[Any]) -> None:
    """消耗或 detached 觀察 process.wait task 的最終結果。"""
    if task.done():
        _consume_extraction_task_exception(task)
    else:
        task.add_done_callback(_consume_extraction_task_exception)


async def _cleanup_extraction_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """取消並 bounded 觀察 extractor pipe tasks，避免 cleanup 例外遺失。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=_EXTRACTION_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        for task in tasks:
            if task.done():
                _consume_extraction_task_exception(task)
            else:
                task.add_done_callback(_consume_extraction_task_exception)
        raise
    for task in done:
        _consume_extraction_task_exception(task)
    for task in pending:
        task.add_done_callback(_consume_extraction_task_exception)


def _consume_extraction_task_exception(task: asyncio.Future[Any]) -> None:
    """消耗 detached extractor task 結果，避免事件迴圈發出未觀察例外。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _parse_json_records(stdout: bytes) -> _ParsedGalleryOutput:
    """解析 pinned 非 JSONL DataJob 頂層 event array。"""
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
        raise AppError(
            "extraction_failed",
            "The extractor returned invalid output.",
            failure_stage="extractor_invalid_output",
        ) from error


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
    exit_code: object,
    target_kind: TargetKind,
    authenticated: bool,
) -> AppError:
    """將 bounded stderr 映射為固定錯誤並附加安全程序證據。"""
    message = stderr.decode("utf-8", errors="replace")
    return _map_extractor_error(
        error_type=None,
        message=message,
        diagnostic_source="stderr",
        exit_code=exit_code,
        target_kind=target_kind,
        authenticated=authenticated,
    )


def _map_json_error_records(
    records: list[dict[str, object]],
    *,
    target_kind: TargetKind,
    authenticated: bool,
) -> list[dict[str, object]]:
    """先映射第一個合法 DataJob error record，再交由 normalizer 處理。"""
    for record in records:
        if "error" in record:
            raw_error_type = record.get("error")
            raw_message = record.get("message")
            raise _map_extractor_error(
                error_type=raw_error_type if isinstance(raw_error_type, str) else None,
                message=raw_message if isinstance(raw_message, str) else "",
                diagnostic_source="datajob_error",
                exit_code=0,
                target_kind=target_kind,
                authenticated=authenticated,
            )
    return records


def _map_extractor_error(
    error_type: str | None,
    message: str,
    *,
    diagnostic_source: ExtractorDiagnosticSource,
    exit_code: object,
    target_kind: TargetKind,
    authenticated: bool,
) -> AppError:
    """分類既有 extractor 錯誤後附加縮減過的來源 metadata。"""
    error = _classify_extractor_error(
        error_type,
        message,
        target_kind=target_kind,
        authenticated=authenticated,
    )
    diagnostic_error_type: ExtractorErrorType
    if diagnostic_source == "stderr":
        diagnostic_error_type = "unknown"
    else:
        diagnostic_error_type = _normalize_extractor_diagnostic_error_type(error_type)
    diagnostic_message = _remove_http_urls(message)
    statuses = sorted(_extract_http_statuses(diagnostic_message))[:8]
    error.extractor_diagnostics = normalize_extractor_diagnostics(
        extractor_diagnostic_source=diagnostic_source,
        extractor_error_type=diagnostic_error_type,
        extractor_exit_code=exit_code,
        extractor_http_statuses=statuses,
    )
    return error


def _normalize_extractor_diagnostic_error_type(error_type: str | None) -> ExtractorErrorType:
    """將 DataJob type 以精確 strip／casefold 白名單映射為固定 enum。"""
    if type(error_type) is not str:
        return "unknown"
    return _EXTRACTOR_DIAGNOSTIC_ERROR_TYPES.get(error_type.strip().casefold(), "unknown")


def _classify_extractor_error(
    error_type: str | None,
    message: str,
    *,
    target_kind: TargetKind,
    authenticated: bool,
) -> AppError:
    """依既有 classifier 將 extractor type 與訊息映射為原應用程式錯誤。"""
    normalized_type = error_type.strip().casefold() if error_type is not None else None
    diagnostic = _remove_http_urls(message)
    statuses = _extract_http_statuses(diagnostic)
    if 429 in statuses:
        return AppError(
            "upstream_rate_limited",
            "The source platform is rate limiting requests.",
            failure_stage="extractor_platform_error",
        )
    if normalized_type not in _KNOWN_ERROR_TYPES and _RATE_LIMIT_PHRASE_PATTERN.search(diagnostic):
        return AppError(
            "upstream_rate_limited",
            "The source platform is rate limiting requests.",
            failure_stage="extractor_platform_error",
        )
    if target_kind == "story":
        return _map_story_extractor_error(
            normalized_type,
            diagnostic,
            statuses=statuses,
            authenticated=authenticated,
        )

    if authenticated and normalized_type == _AUTHENTICATION_ERROR_TYPE:
        return AppError(
            "platform_authentication_failed",
            "The configured platform session is unavailable. Contact the service operator.",
            failure_stage="extractor_platform_error",
        )
    if normalized_type == _AUTH_REQUIRED_ERROR_TYPE:
        if authenticated:
            return AppError(
                "platform_authentication_failed",
                "The configured platform session is unavailable. Contact the service operator.",
                failure_stage="extractor_platform_error",
            )
        return AppError(
            "post_unavailable",
            "This post is not available anonymously.",
            failure_stage="extractor_platform_error",
        )
    if not authenticated and normalized_type == _AUTHENTICATION_ERROR_TYPE:
        return AppError(
            "post_unavailable",
            "This post is not available anonymously.",
            failure_stage="extractor_platform_error",
        )
    explicit_authentication_failure = (
        _EXPLICIT_SESSION_FAILURE_PATTERN.search(diagnostic) is not None
        or _AUTHENTICATION_FAILURE_PATTERN.search(diagnostic) is not None
    )
    broad_post_authentication_failure = _BROAD_AUTHENTICATION_PATTERN.search(diagnostic) is not None
    if authenticated and (explicit_authentication_failure or broad_post_authentication_failure):
        return AppError(
            "platform_authentication_failed",
            "The configured platform session is unavailable. Contact the service operator.",
            failure_stage="extractor_platform_error",
        )
    if not authenticated and _AUTHENTICATION_FAILURE_PATTERN.search(diagnostic) is not None:
        return AppError(
            "post_unavailable",
            "This post is not available anonymously.",
            failure_stage="extractor_platform_error",
        )
    if _POST_UNAVAILABLE_PATTERN.search(diagnostic) is not None:
        return AppError(
            "post_unavailable",
            "This post is not available anonymously.",
            failure_stage="extractor_platform_error",
        )
    return AppError(
        "extraction_failed",
        "The source platform could not be extracted.",
        failure_stage="extractor_process_unclassified",
    )


def _map_story_extractor_error(
    normalized_type: str | None,
    diagnostic: str,
    *,
    statuses: set[int],
    authenticated: bool,
) -> AppError:
    """依明確 diagnostic 證據與 Cookie 配置狀態分類單則 Story 錯誤。"""
    if normalized_type == _NOT_FOUND_ERROR_TYPE or 404 in statuses:
        return AppError(
            "story_unavailable",
            "This Story is unavailable.",
            failure_stage="extractor_platform_error",
        )

    authentication_failure = (
        normalized_type in {_AUTH_REQUIRED_ERROR_TYPE, _AUTHENTICATION_ERROR_TYPE}
        or _STORY_AUTHENTICATION_PATTERN.search(diagnostic) is not None
        or _EXPLICIT_SESSION_FAILURE_PATTERN.search(diagnostic) is not None
        or _AUTHENTICATION_FAILURE_PATTERN.search(diagnostic) is not None
    )
    if authentication_failure:
        if authenticated:
            return AppError(
                "platform_authentication_failed",
                "The configured platform session is unavailable. Contact the service operator.",
                failure_stage="extractor_platform_error",
            )
        return AppError(
            "story_auth_required",
            "This Instagram Story requires an authenticated session.",
            failure_stage="extractor_platform_error",
        )

    if _STORY_UNAVAILABLE_PATTERN.search(diagnostic) is not None:
        return AppError(
            "story_unavailable",
            "This Story is unavailable.",
            failure_stage="extractor_platform_error",
        )

    if statuses.intersection({401, 403}):
        if authenticated:
            return AppError(
                "extraction_failed",
                "The source platform could not be extracted.",
                failure_stage="extractor_process_unclassified",
            )
        return AppError(
            "story_auth_required",
            "This Instagram Story requires an authenticated session.",
            failure_stage="extractor_platform_error",
        )

    return AppError(
        "extraction_failed",
        "The source platform could not be extracted.",
        failure_stage="extractor_process_unclassified",
    )


def _remove_http_urls(message: str) -> str:
    """Remove HTTP and HTTPS URLs before diagnostic phrase classification."""
    return _URL_PATTERN.sub("", message)


def _extract_http_statuses(message: str) -> set[int]:
    """從 HTTP／status 前綴或明確配對的狀態碼與原因片語取得狀態。"""
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
