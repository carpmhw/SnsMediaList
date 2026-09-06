"""Extraction and media download API routes."""

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from time import monotonic, perf_counter
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

from ..api.limits import Lease, RequestLimiter, client_identity
from ..errors import AppError
from ..logging_config import build_event
from ..models import ExtractionResponse, PrivateMediaRecord
from ..network.media_client import (
    MediaClient,
    MediaResponse,
    MediaTruncatedError,
    build_forward_response_headers,
    build_preview_headers,
    iter_validated_body,
    validate_resume_response,
)
from ..services.extraction_service import ExtractionService
from ..services.thumbnail import ThumbnailGenerator, validate_thumbnail_media_class
from ..services.thumbnail_cache import ThumbnailCoordinator

logger = logging.getLogger("sns_media_list")

_STREAM_CLEANUP_TIMEOUT_SECONDS = 1.0
_DEFAULT_STREAM_SEND_TIMEOUT_SECONDS = 30.0

_DOWNLOAD_REASON_CODES = frozenset(
    {
        "idle_timeout",
        "size_limit",
        "client_disconnect",
        "upstream_truncation",
        "upstream_validation",
        "unexpected_upstream_failure",
    }
)


class StreamAborted(RuntimeError):
    """以無敏感資訊的訊號要求 ASGI server 中止未完成傳輸。"""


class DeadlineStreamingResponse(StreamingResponse):
    """Stream one response while bounding upstream and downstream lifetime."""

    def __init__(
        self,
        *args: Any,
        deadline: float | None,
        send_timeout: float | None = None,
        cleanup: Callable[[], Awaitable[None]] | None = None,
        on_stream_error: Callable[[BaseException], None] | None = None,
        on_stream_complete: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """保存整體期限、單次 send idle bound、清理函式與串流回呼。"""
        super().__init__(*args, **kwargs)
        self.deadline = deadline
        self.send_timeout = send_timeout
        self._cleanup = cleanup
        self._on_stream_error = on_stream_error
        self._on_stream_complete = on_stream_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """在完整生命週期到期或下游中斷時完成清理並通知回呼。"""
        stream_error: BaseException | None = None
        stream_completed = False
        disconnect_received = False
        response_started = False
        response_completed = False
        deadline_timeout = False
        caller_cancelled: asyncio.CancelledError | None = None

        async def tracked_receive() -> Message:
            """追蹤 ASGI request lifecycle 是否收到 client disconnect。"""
            nonlocal disconnect_received
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnect_received = True
            return message

        async def tracked_send(message: Message) -> None:
            """以單次 downstream send idle bound 傳送訊息並標記 response lifecycle。"""

            def mark_sent() -> None:
                """只在底層 send 成功後更新 response start/completion 狀態。"""
                nonlocal response_started, response_completed
                if message["type"] == "http.response.start":
                    response_started = True
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    response_completed = True

            if self.send_timeout is None:
                await send(message)
            else:
                send_task = _ensure_operation_task(send(message))
                try:
                    done, _pending = await asyncio.wait(
                        {send_task},
                        timeout=max(0.0, self.send_timeout),
                    )
                except asyncio.CancelledError:
                    if send_task.done() and not send_task.cancelled():
                        try:
                            send_task.result()
                        except BaseException:
                            _consume_task_exception(send_task)
                            raise
                        mark_sent()
                        if message["type"] == "http.response.body" and not message.get(
                            "more_body", False
                        ):
                            return
                    _cancel_task_without_waiting(send_task)
                    raise
                if send_task not in done:
                    _cancel_task_without_waiting(send_task)
                    raise AppError(
                        "upstream_media_invalid",
                        "The downstream response timed out.",
                    )
                send_task.result()
            mark_sent()

        try:
            if self.deadline is None:
                await super().__call__(scope, tracked_receive, tracked_send)
            else:
                stream_task = asyncio.create_task(
                    super().__call__(scope, tracked_receive, tracked_send)
                )
                deadline_task = asyncio.create_task(
                    asyncio.sleep(max(0.0, self.deadline - monotonic()))
                )
                try:
                    done, _pending = await asyncio.wait(
                        {stream_task, deadline_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    _cancel_task_without_waiting(stream_task)
                    _cancel_task_without_waiting(deadline_task)
                    raise
                if stream_task in done:
                    _cancel_task_without_waiting(deadline_task)
                    await stream_task
                else:
                    deadline_timeout = True
                    stream_task.cancel()
                    try:
                        done, _pending = await asyncio.wait(
                            {stream_task},
                            timeout=_STREAM_CLEANUP_TIMEOUT_SECONDS,
                        )
                    except asyncio.CancelledError:
                        _cancel_task_without_waiting(stream_task)
                        raise
                    if stream_task in done:
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass
                        except BaseException as error:
                            stream_error = error
                            deadline_timeout = False
                    else:
                        _cancel_task_without_waiting(stream_task)
            if stream_error is None:
                if deadline_timeout:
                    stream_error = AppError(
                        "upstream_media_invalid",
                        "The media response exceeded its deadline.",
                    )
                elif disconnect_received and not response_completed:
                    stream_error = ClientDisconnect()
                else:
                    stream_completed = True
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                caller_cancelled = error
            stream_error = error

        cleanup_error: BaseException | None = None
        try:
            await self._run_cleanup()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                if caller_cancelled is None:
                    caller_cancelled = error
            cleanup_error = error

        if stream_error is None:
            stream_error = cleanup_error
        if stream_error is not None:
            if self._on_stream_error is not None:
                try:
                    self._on_stream_error(stream_error)
                except Exception:
                    pass
        elif stream_completed and self._on_stream_complete is not None:
            try:
                self._on_stream_complete()
            except Exception:
                pass

        if caller_cancelled is not None:
            raise caller_cancelled
        if isinstance(stream_error, ClientDisconnect):
            raise ClientDisconnect
        if stream_error is not None and not response_started:
            raise stream_error
        if stream_error is not None and response_started and not response_completed:
            # 離開原始 except 區塊才建立訊號，避免 framework 記錄上游例外鏈。
            raise StreamAborted("Media stream aborted.") from None

    async def _run_cleanup(self) -> None:
        """Run response cleanup once even when streaming never starts."""
        cleanup = self._cleanup
        self._cleanup = None
        if cleanup is not None:
            await cleanup()


class ExtractionRequest(BaseModel):
    """Represent the only input accepted by the extraction endpoint."""

    url: str = Field(min_length=1, max_length=2048)


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    """消耗脫離主流程的 task 例外，避免 cancellation-resistant task 警告。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _observe_task_result(
    task: asyncio.Future[Any],
    detached_result_cleanup: Callable[[Any], Awaitable[None]] | None,
) -> None:
    """觀察 detached task 結果，必要時啟動其 bounded result cleanup。"""
    if task.cancelled():
        return
    try:
        result = task.result()
    except BaseException:
        return
    if detached_result_cleanup is None:
        return
    try:
        cleanup_task = asyncio.ensure_future(detached_result_cleanup(result))
    except BaseException:
        return
    cleanup_task.add_done_callback(_consume_task_exception)


def _cancel_task_without_waiting(
    task: asyncio.Future[Any],
    *,
    detached_result_cleanup: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """取消清理 task 並觀察結果，必要時處理 cancellation-resistant 成功值。"""
    if task.done():
        _observe_task_result(task, detached_result_cleanup)
        return
    task.cancel()
    task.add_done_callback(
        lambda completed_task: _observe_task_result(
            completed_task,
            detached_result_cleanup,
        )
    )


def _ensure_operation_task[T](awaitable: Awaitable[T]) -> asyncio.Future[T]:
    """將任意 awaitable 確保成可獨立取消與脫離的 operation task。"""
    return asyncio.ensure_future(awaitable)


def _dispose_expired_awaitable(
    awaitable: Awaitable[Any],
    *,
    detached_result_cleanup: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """處置 expired deadline 的 coroutine 或 Future，避免遺留背景執行。"""
    if inspect.iscoroutine(awaitable):
        try:
            awaitable.close()
        except BaseException:
            pass
        return
    if not asyncio.isfuture(awaitable):
        return
    future = awaitable
    if future.done():
        _observe_task_result(future, detached_result_cleanup)
        return
    future.cancel()
    future.add_done_callback(
        lambda completed_task: _observe_task_result(
            completed_task,
            detached_result_cleanup,
        )
    )


def build_router(
    service: ExtractionService,
    media_client: MediaClient,
    *,
    limiter: RequestLimiter,
    trusted_proxy_cidrs: Iterable[str],
    media_response_timeout_seconds: float,
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
) -> APIRouter:
    """建立注入服務相依性的 API 路由。"""
    router = APIRouter(prefix="/api")

    @router.post("/extractions")
    async def create_extraction(request: Request, payload: ExtractionRequest) -> ExtractionResponse:
        """Extract a public post and return its normalized media list."""
        lease = limiter.acquire_extraction(_request_client_identity(request, trusted_proxy_cidrs))
        async with lease:
            started = perf_counter()
            result = await service.extract(payload.url)
        logger.info(
            "extraction_complete",
            extra={
                "event": build_event(
                    request_id=getattr(request.state, "request_id", "unknown"),
                    platform=result.platform,
                    outcome="success",
                    duration_ms=(perf_counter() - started) * 1000,
                    item_count=len(result.media),
                )
            },
        )
        return result

    @router.get("/media/{token}/download")
    async def download_media(request: Request, token: str) -> StreamingResponse:
        """將一個已授權的媒體資源以附件串流回傳。"""
        record = service.token_store.get(token, "download")
        lease = limiter.acquire_download(_request_client_identity(request, trusted_proxy_cidrs))
        return await _stream_media(
            record,
            media_client,
            preview=False,
            lease=lease,
            response_timeout=None,
            request_id=_request_id(request),
        )

    @router.head("/media/{token}/download")
    async def preflight_download(token: str) -> Response:
        """Validate a download token without contacting or reading upstream media."""
        service.token_store.get(token, "download")
        return Response(status_code=204)

    @router.get("/media/{token}/preview")
    async def preview_media(request: Request, token: str) -> Response:
        """Proxy or generate one authorized preview with passive browser headers."""
        record = service.token_store.get(token, "preview")
        if record.preview_mode == "generated" and not service.settings.generated_previews_enabled:
            raise AppError("token_not_found", "The media token is not available.")
        lease = limiter.acquire_download(_request_client_identity(request, trusted_proxy_cidrs))
        if record.preview_mode == "generated":
            return await _generate_preview(
                record,
                media_client,
                thumbnail_generator,
                thumbnail_coordinator,
                lease=lease,
                response_timeout=media_response_timeout_seconds,
            )
        return await _stream_media(
            record,
            media_client,
            preview=True,
            lease=lease,
            response_timeout=media_response_timeout_seconds,
        )

    return router


def _request_client_identity(request: Request, trusted_proxy_cidrs: Iterable[str]) -> str:
    """Reuse the pre-parsed client identity established by the ASGI boundary."""
    identity = getattr(request.state, "client_identity", None)
    if isinstance(identity, str):
        return identity
    return client_identity(
        request.client.host if request.client else "unknown",
        request.headers.get("x-forwarded-for"),
        trusted_proxy_cidrs,
    )


def _request_id(request: Request) -> str:
    """從 ASGI state 取出格式受限的 request ID，無效值改用 unknown。"""
    return _safe_request_id(getattr(request.state, "request_id", None))


def _safe_request_id(value: object) -> str:
    """只接受 middleware 產生的 ASCII request ID，避免把輸入內容寫入事件。"""
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isascii():
        return "unknown"
    if not all(char.isalnum() or char in "-_" for char in value):
        return "unknown"
    return value


def _download_metadata(record: PrivateMediaRecord, request_id: str) -> tuple[str, str, str]:
    """從 token 內的 allowlist metadata 建立安全下載事件識別資料。"""
    platform = record.platform if record.platform in {"instagram", "x"} else "unknown"
    media_class = record.media_class if record.media_class in {"image", "video"} else "unknown"
    return _safe_request_id(request_id), platform, media_class


def _download_reason_code(error: BaseException) -> str:
    """將下載例外映射為不含原始細節的 bounded reason code。"""
    if isinstance(error, ClientDisconnect | asyncio.CancelledError | GeneratorExit):
        return "client_disconnect"
    if isinstance(error, TimeoutError):
        return "idle_timeout"
    if isinstance(error, asyncio.IncompleteReadError):
        return "upstream_truncation"
    if isinstance(error, AppError):
        message = error.message.casefold()
        if "timed out" in message or "deadline" in message:
            return "idle_timeout"
        if "size limit" in message:
            return "size_limit"
        if "truncated" in message:
            return "upstream_truncation"
        if "could not be read" in message or "request failed" in message:
            return "unexpected_upstream_failure"
        return "upstream_validation"
    return "unexpected_upstream_failure"


def _log_download_event(
    message: str,
    *,
    request_id: str,
    platform: str,
    media_class: str,
    outcome: str,
    duration_ms: float,
    bytes_streamed: int,
    reason_code: str | None = None,
    resume_attempt: int | None = None,
) -> None:
    """以安全欄位記錄下載事件，且不讓 logging 例外影響下載流程。"""
    if reason_code is not None and reason_code not in _DOWNLOAD_REASON_CODES:
        reason_code = "unexpected_upstream_failure"
    try:
        logger.info(
            message,
            extra={
                "event": build_event(
                    request_id=request_id,
                    platform=platform,
                    media_class=media_class,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    bytes_streamed=bytes_streamed,
                    reason_code=reason_code,
                    resume_attempt=resume_attempt,
                )
            },
        )
    except Exception:
        pass


def _safe_log_download_event(*args: Any, **kwargs: Any) -> None:
    """隔離結構化 logging 例外，避免影響下載主流程與資源清理。"""
    try:
        _log_download_event(*args, **kwargs)
    except Exception:
        pass


async def _stream_media(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    *,
    preview: bool,
    lease: Lease,
    response_timeout: float | None = None,
    request_id: str = "unknown",
) -> StreamingResponse:
    """取得、預檢並串流一筆私有媒體，同時記錄安全生命週期事件。"""
    source_url = record.source_url
    request_headers = record.request_headers
    filename = record.filename
    deadline = monotonic() + response_timeout if response_timeout is not None else None
    event_request_id, platform, media_class = _download_metadata(record, request_id)
    started = perf_counter()
    bytes_streamed = 0
    terminal_logged = False

    def log_terminal(error: BaseException | None) -> None:
        """在清理完成後記錄一次下載完成或失敗事件。"""
        nonlocal terminal_logged
        if preview:
            return
        if terminal_logged:
            return
        terminal_logged = True
        if error is None:
            _safe_log_download_event(
                "media_download_completed",
                request_id=event_request_id,
                platform=platform,
                media_class=media_class,
                outcome="success",
                duration_ms=(perf_counter() - started) * 1000,
                bytes_streamed=bytes_streamed,
            )
            return
        reason_code = _download_reason_code(error)
        message = (
            "media_download_aborted"
            if reason_code == "client_disconnect"
            else "media_download_failed"
        )
        _safe_log_download_event(
            message,
            request_id=event_request_id,
            platform=platform,
            media_class=media_class,
            outcome="aborted" if reason_code == "client_disconnect" else "failed",
            duration_ms=(perf_counter() - started) * 1000,
            bytes_streamed=bytes_streamed,
            reason_code=reason_code,
        )

    def log_completed() -> None:
        """在完整 downstream response 成功返回後記錄 completed 事件。"""
        log_terminal(None)

    def log_resume_event(
        message: str,
        *,
        outcome: str,
        error: BaseException | None = None,
    ) -> None:
        """記錄只含 bounded 欄位的單次 Range resume telemetry。"""
        if preview:
            return
        _safe_log_download_event(
            message,
            request_id=event_request_id,
            platform=platform,
            media_class=media_class,
            outcome=outcome,
            duration_ms=(perf_counter() - started) * 1000,
            bytes_streamed=bytes_streamed,
            reason_code=None if error is None else _download_reason_code(error),
            resume_attempt=1,
        )

    async def cleanup_failed_response(response: MediaResponse | None) -> None:
        """盡力 close/release 失敗路徑資源，但不覆蓋原始例外。"""
        if response is not None:
            try:
                timeout = None if deadline is None else max(0.0, deadline - monotonic())
                await _close_response_with_timeout(response, timeout=timeout)
            except BaseException:
                pass
        try:
            await lease.release()
        except BaseException:
            pass

    async def cleanup_detached_fetch(response: MediaResponse) -> None:
        """在 fetch 脫離後以 bounded timeout 關閉最終成功的 response。"""
        timeout = None if deadline is None else max(0.0, deadline - monotonic())
        await _close_response_with_timeout(response, timeout=timeout)

    if not preview:
        _safe_log_download_event(
            "media_download_started",
            request_id=event_request_id,
            platform=platform,
            media_class=media_class,
            outcome="started",
            duration_ms=0.0,
            bytes_streamed=0,
        )
    try:
        response = await _await_with_deadline(
            media_client.fetch(source_url, headers=request_headers),
            deadline,
            detached_result_cleanup=cleanup_detached_fetch,
        )
    except BaseException as error:
        await cleanup_failed_response(None)
        log_terminal(error)
        raise
    try:
        original_total_length = response.content_length
    except BaseException as error:
        await cleanup_failed_response(response)
        log_terminal(error)
        raise
    original_content_type = response.headers.get("content-type", "")
    iterator = iter_validated_body(
        response,
        preview=preview,
        expected_media_class=record.media_class,
    )
    try:
        first_chunk = await _await_with_deadline(anext(iterator), deadline)
    except StopAsyncIteration as error:
        empty_body_error = AppError("upstream_media_invalid", "The upstream media body was empty.")
        await cleanup_failed_response(response)
        log_terminal(empty_body_error)
        raise empty_body_error from error
    except BaseException as error:
        await cleanup_failed_response(response)
        log_terminal(error)
        raise

    open_responses = [response]
    closed_response_ids: set[int] = set()
    cleanup_errors: list[BaseException] = []
    cleaned = False
    send_timeout = getattr(media_client, "read_timeout", _DEFAULT_STREAM_SEND_TIMEOUT_SECONDS)

    async def close_response_once(response_to_close: MediaResponse) -> None:
        """以 bounded timeout 關閉單一 response，並保留 cleanup error。"""
        response_id = id(response_to_close)
        if response_id in closed_response_ids:
            return
        closed_response_ids.add(response_id)
        try:
            timeout = None if deadline is None else max(0.0, deadline - monotonic())
            await _close_response_with_timeout(response_to_close, timeout=timeout)
        except BaseException as error:
            cleanup_errors.append(error)

    async def cleanup() -> None:
        """只清理所有 upstream response 一次並釋放下載 lease。"""
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        for response_to_close in open_responses:
            await close_response_once(response_to_close)
        try:
            await lease.release()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            raise cleanup_errors[0]

    async def body() -> AsyncIterator[bytes]:
        """依序送出預檢 chunk 與後續 upstream bytes，並記錄終止原因。"""
        nonlocal bytes_streamed
        primary_error: BaseException | None = None
        current_iterator = iterator
        resume_attempted = False
        try:
            yield first_chunk
            bytes_streamed += len(first_chunk)
            while True:
                try:
                    chunk = await _await_with_deadline(anext(current_iterator), deadline)
                except MediaTruncatedError as truncation_error:
                    can_resume = (
                        not preview
                        and not resume_attempted
                        and record.platform == "x"
                        and record.media_class == "video"
                        and response.status_code == 200
                        and original_total_length is not None
                        and 0 < bytes_streamed < original_total_length
                    )
                    if not can_resume:
                        if resume_attempted:
                            log_resume_event(
                                "media_download_resume_failed",
                                outcome="failed",
                                error=truncation_error,
                            )
                        raise
                    if original_total_length is None:
                        raise truncation_error
                    resume_attempted = True
                    resume_offset = bytes_streamed
                    log_resume_event(
                        "media_download_resume_attempted",
                        outcome="attempted",
                    )
                    await close_response_once(response)
                    try:
                        resumed_response = await _await_with_deadline(
                            media_client.fetch(
                                source_url,
                                headers=request_headers,
                                range_start=resume_offset,
                            ),
                            deadline,
                            detached_result_cleanup=cleanup_detached_fetch,
                        )
                        open_responses.append(resumed_response)
                        validate_resume_response(
                            resumed_response,
                            requested_offset=resume_offset,
                            original_total_length=original_total_length,
                            original_content_type=original_content_type,
                        )
                        current_iterator = iter_validated_body(
                            resumed_response,
                            expected_media_class="video",
                            max_bytes=original_total_length - resume_offset,
                        )
                    except BaseException as resume_error:
                        log_resume_event(
                            "media_download_resume_failed",
                            outcome="failed",
                            error=resume_error,
                        )
                        raise
                    continue
                except StopAsyncIteration:
                    break
                except BaseException as error:
                    if resume_attempted and not isinstance(
                        error, asyncio.CancelledError | ClientDisconnect
                    ):
                        log_resume_event(
                            "media_download_resume_failed",
                            outcome="failed",
                            error=error,
                        )
                    raise
                yield chunk
                bytes_streamed += len(chunk)
            if resume_attempted and original_total_length is not None:
                if bytes_streamed < original_total_length:
                    incomplete_error = MediaTruncatedError(
                        "upstream_media_invalid", "The media response body was truncated."
                    )
                    log_resume_event(
                        "media_download_resume_failed",
                        outcome="failed",
                        error=incomplete_error,
                    )
                    raise incomplete_error
                if bytes_streamed > original_total_length:
                    oversized_error = AppError(
                        "upstream_media_invalid", "The resumed media body is invalid."
                    )
                    log_resume_event(
                        "media_download_resume_failed",
                        outcome="failed",
                        error=oversized_error,
                    )
                    raise oversized_error
                log_resume_event(
                    "media_download_resume_succeeded",
                    outcome="success",
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                await cleanup()
            except BaseException as error:
                cleanup_error = error
            if primary_error is not None:
                log_terminal(primary_error)
            elif cleanup_error is not None:
                log_terminal(cleanup_error)
            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

    try:
        headers = build_forward_response_headers(response, filename=filename, preview=preview)
    except BaseException as error:
        await cleanup_failed_response(response)
        log_terminal(error)
        raise
    return DeadlineStreamingResponse(
        body(),
        headers=headers,
        media_type=response.headers.get("content-type"),
        deadline=deadline,
        cleanup=cleanup,
        send_timeout=send_timeout,
        on_stream_error=log_terminal if not preview else None,
        on_stream_complete=log_completed if not preview else None,
    )


async def _await_with_deadline[T](
    awaitable: Awaitable[T],
    deadline: float | None,
    *,
    timeout_message: str = "The media response exceeded its deadline.",
    detached_result_cleanup: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """在可選 deadline 前等待 operation，並以安全訊息脫離抗取消 operation。"""
    remaining: float | None = None
    if deadline is not None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            _dispose_expired_awaitable(
                awaitable,
                detached_result_cleanup=detached_result_cleanup,
            )
            raise AppError("upstream_media_invalid", timeout_message)
    operation_task = _ensure_operation_task(awaitable)
    if remaining is None:
        try:
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            _cancel_task_without_waiting(
                operation_task,
                detached_result_cleanup=detached_result_cleanup,
            )
            raise
    try:
        done, _pending = await asyncio.wait({operation_task}, timeout=remaining)
    except asyncio.CancelledError:
        _cancel_task_without_waiting(
            operation_task,
            detached_result_cleanup=detached_result_cleanup,
        )
        raise
    if operation_task in done:
        return await operation_task
    _cancel_task_without_waiting(
        operation_task,
        detached_result_cleanup=detached_result_cleanup,
    )
    raise AppError("upstream_media_invalid", timeout_message)


async def _generate_preview(
    record: PrivateMediaRecord,
    media_client: MediaClient,
    thumbnail_generator: ThumbnailGenerator,
    thumbnail_coordinator: ThumbnailCoordinator,
    *,
    lease: Lease,
    response_timeout: float | None = None,
) -> Response:
    """產生一個緩衝 JPEG，並將 lease 保留至 response 完成。"""
    deadline = monotonic() + response_timeout if response_timeout is not None else None

    async def release_lease() -> None:
        """以 best-effort 方式釋放失敗路徑的 lease，避免覆蓋原始例外。"""
        try:
            await lease.release()
        except BaseException:
            pass

    async def generate() -> bytes:
        """在完整 preview deadline 內取得 source 並產生 JPEG。"""

        async def fetch_and_generate() -> bytes:
            """取得並驗證 source，且由 route 統一負責 response close ownership。"""
            response: MediaResponse | None = None
            primary_error: BaseException | None = None
            try:
                response = await media_client.fetch(
                    record.source_url, headers=record.request_headers
                )
                if not 200 <= response.status_code < 300:
                    raise AppError(
                        "upstream_media_invalid", "The upstream media response was not successful."
                    )
                validate_thumbnail_media_class(
                    response.headers.get("content-type", ""), record.media_class
                )
                return await thumbnail_generator.generate(response)
            except AppError as error:
                primary_error = error
                raise
            except asyncio.CancelledError as error:
                primary_error = error
                raise
            except Exception as error:
                primary_error = AppError(
                    "upstream_media_invalid", "The upstream media request failed."
                )
                raise primary_error from error
            finally:
                if response is not None:
                    try:
                        await _close_response_with_timeout(response, timeout=timeout)
                    except BaseException:
                        if primary_error is None:
                            raise

        timeout = getattr(thumbnail_generator, "timeout_seconds", None)
        if timeout is None:
            return await fetch_and_generate()
        return await _await_with_deadline(
            fetch_and_generate(),
            monotonic() + timeout,
            timeout_message="Thumbnail generation timed out.",
        )

    try:
        data = await _await_with_deadline(
            thumbnail_coordinator.get_or_generate(
                record.token, expires_at=record.expires_at, factory=generate
            ),
            deadline,
        )
    except BaseException:
        await release_lease()
        raise
    try:
        headers = build_preview_headers(record.filename)
        headers["Content-Type"] = "image/jpeg"
        headers["Content-Length"] = str(len(data))
        send_timeout = getattr(media_client, "read_timeout", _DEFAULT_STREAM_SEND_TIMEOUT_SECONDS)
        return DeadlineStreamingResponse(
            [data],
            headers=headers,
            media_type="image/jpeg",
            deadline=deadline,
            send_timeout=send_timeout,
            cleanup=release_lease,
        )
    except BaseException:
        await release_lease()
        raise


async def _close_response_with_timeout(
    response: MediaResponse,
    *,
    timeout: float | None,
) -> None:
    """以有限等待關閉 upstream response，並確保取消的清理 task 不殘留。"""
    cleanup_timeout = min(
        max(0.0, timeout) if timeout is not None else _STREAM_CLEANUP_TIMEOUT_SECONDS,
        _STREAM_CLEANUP_TIMEOUT_SECONDS,
    )
    task = asyncio.create_task(response.close())
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(0.0, cleanup_timeout))
    except asyncio.CancelledError:
        _cancel_task_without_waiting(task)
        raise
    if not done:
        _cancel_task_without_waiting(task)
        return
    await task
