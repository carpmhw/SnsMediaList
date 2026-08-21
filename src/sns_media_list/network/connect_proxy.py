"""Loopback HTTP CONNECT proxy used to restrict extractor egress."""

import asyncio
import math
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from ..errors import AppError
from .dns import DestinationPolicy

_DEFAULT_OPERATION_TIMEOUT_SECONDS = 45.0
_RELAY_CLEANUP_TIMEOUT_SECONDS = 0.1
_CLIENT_SHUTDOWN_TIMEOUT_SECONDS = 0.1


def parse_connect_request(request: bytes) -> tuple[str, int]:
    """Parse a CONNECT request and accept only hostname:443 targets."""
    try:
        request_line = request.split(b"\r\n", 1)[0].decode("ascii")
        method, target, version = request_line.split(" ", 2)
        hostname, port_text = target.rsplit(":", 1)
        port = int(port_text)
    except (UnicodeDecodeError, ValueError) as error:
        raise AppError("unsafe_destination", "Only HTTPS CONNECT targets are allowed.") from error

    if method != "CONNECT" or version != "HTTP/1.1" or not hostname or port != 443:
        raise AppError("unsafe_destination", "Only HTTPS CONNECT targets are allowed.")
    if any(character in hostname for character in "[]/\\?#@ "):
        raise AppError("unsafe_destination", "The CONNECT hostname is invalid.")
    return hostname.lower().rstrip("."), port


class ConnectProxy:
    """Tunnel extractor CONNECT requests only to policy-approved destinations."""

    def __init__(
        self,
        policy: DestinationPolicy,
        *,
        max_header_bytes: int = 8192,
        operation_timeout_seconds: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """建立具有限制標頭大小與 bounded operation timeout 的 CONNECT proxy。"""
        try:
            operation_timeout = float(operation_timeout_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("operation_timeout_seconds must be finite and positive") from error
        if not math.isfinite(operation_timeout) or operation_timeout <= 0:
            raise ValueError("operation_timeout_seconds must be finite and positive")
        self.policy = policy
        self.max_header_bytes = max_header_bytes
        self.operation_timeout_seconds = operation_timeout
        self._client_tasks: set[asyncio.Task[None]] = set()

    async def serve(self, host: str, port: int) -> asyncio.AbstractServer:
        """Start the loopback proxy server and return its server handle."""
        return await asyncio.start_server(self.handle_client, host, port)

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """驗證單一 CONNECT request 並 relay encrypted tunnel。"""
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        upstream_writer: asyncio.StreamWriter | None = None
        tunnel_established = False
        deadline = asyncio.get_running_loop().time() + self.operation_timeout_seconds
        try:
            request = await _run_with_deadline(reader.readuntil(b"\r\n\r\n"), deadline)
            if len(request) > self.max_header_bytes:
                raise AppError("unsafe_destination", "The CONNECT request is too large.")
            hostname, port = parse_connect_request(request)
            target = await _run_with_deadline(
                asyncio.to_thread(self.policy.validate, hostname, port),
                deadline,
            )
            upstream_reader, connected_writer = await _run_with_deadline(
                asyncio.open_connection(str(target.address), target.port),
                deadline,
                late_result_observer=_observe_late_connection,
            )
            upstream_writer = connected_writer
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunnel_established = True
            await _run_with_deadline(writer.drain(), deadline)
            await _run_with_deadline(
                _relay(reader, writer, upstream_reader, connected_writer), deadline
            )
        except (AppError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            if not tunnel_established:
                try:
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    await _run_with_deadline(writer.drain(), deadline)
                except (OSError, TimeoutError):
                    pass
        finally:
            try:
                await _close_writers(writer, upstream_writer)
            finally:
                if task is not None:
                    self._client_tasks.discard(task)

    async def close_clients(self) -> None:
        """取消 active CONNECT handlers，並等待其 bounded cleanup 完成。"""
        tasks = tuple(self._client_tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=_CLIENT_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except BaseException:
            _observe_tasks(set(tasks))
            raise
        _observe_tasks(done | pending)


async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
    """等待 writer 關閉，讓 caller 以 task observer 消耗 transport 例外。"""
    await writer.wait_closed()


async def _close_writers(
    client_writer: asyncio.StreamWriter,
    upstream_writer: asyncio.StreamWriter | None,
) -> None:
    """獨立發起兩側 close 並 bounded 觀察 wait_closed 結果。"""
    wait_tasks: set[asyncio.Task[None]] = set()
    for writer in (upstream_writer, client_writer):
        if writer is None:
            continue
        try:
            writer.close()
        except (OSError, RuntimeError):
            continue
        try:
            wait_tasks.add(asyncio.create_task(_wait_writer_closed(writer)))
        except BaseException:
            continue

    if not wait_tasks:
        return
    try:
        done, pending = await asyncio.wait(
            wait_tasks,
            timeout=_RELAY_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        for task in wait_tasks:
            if task.done():
                _consume_task_exception(task)
            else:
                task.add_done_callback(_consume_task_exception)
        raise
    for task in done:
        _consume_task_exception(task)
    for task in pending:
        task.add_done_callback(_consume_task_exception)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy bytes in both directions until either side closes."""
    copy_tasks = {
        asyncio.create_task(_copy_stream(client_reader, upstream_writer)),
        asyncio.create_task(_copy_stream(upstream_reader, client_writer)),
    }
    try:
        done, pending = await asyncio.wait(copy_tasks, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        await _cancel_copy_tasks(copy_tasks)
        raise

    copy_error: BaseException | None = None
    for task in done:
        try:
            task.result()
        except BaseException as error:
            if copy_error is None:
                copy_error = error
    await _cancel_copy_tasks(pending)
    if copy_error is not None:
        raise copy_error


async def _cancel_copy_tasks(tasks: set[asyncio.Task[None]]) -> None:
    """取消 relay copy tasks，bounded 等待後觀察仍未完成的 task。"""
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=_RELAY_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        for task in tasks:
            if task.done():
                _consume_task_exception(task)
            else:
                task.add_done_callback(_consume_task_exception)
        raise
    for task in done:
        _consume_task_exception(task)
    for task in pending:
        task.add_done_callback(_consume_task_exception)


def _consume_task_exception(task: asyncio.Future[None]) -> None:
    """消耗 detached copy task 結果，避免取消後遺留未處理例外。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _observe_tasks(tasks: Iterable[asyncio.Future[Any]]) -> None:
    """立即消耗已完成 task 結果，並為 pending task 註冊 detached observer。"""
    for task in tasks:
        if task.done():
            _consume_task_exception(task)
        else:
            task.add_done_callback(_consume_task_exception)


def _cancel_and_observe(
    task: asyncio.Task[Any],
    observer: Callable[[asyncio.Future[Any]], None],
) -> None:
    """取消 operation task，並確保其 late result 或例外會被觀察。"""
    task.cancel()
    task.add_done_callback(observer)


async def _run_with_deadline[OperationResult](
    operation: Coroutine[Any, Any, OperationResult],
    deadline: float,
    *,
    late_result_observer: Callable[[asyncio.Future[Any]], None] | None = None,
) -> OperationResult:
    """以 task 加 bounded asyncio.wait 執行 operation，避免 cancellation-resistant awaitable
    卡死 caller。
    """
    task = asyncio.create_task(operation)
    observer = late_result_observer or _consume_task_exception
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        _cancel_and_observe(task, observer)
        raise TimeoutError("CONNECT operation timed out")
    try:
        done, _pending = await asyncio.wait({task}, timeout=remaining)
    except BaseException:
        _cancel_and_observe(task, observer)
        raise
    if not done:
        _cancel_and_observe(task, observer)
        raise TimeoutError("CONNECT operation timed out")
    return task.result()


def _observe_late_connection(task: asyncio.Future[Any]) -> None:
    """觀察逾時後才完成的 open_connection，關閉 late upstream writer 並 bounded 等待。"""
    if task.cancelled():
        return
    try:
        _reader, writer = task.result()
    except BaseException:
        return
    try:
        writer.close()
    except BaseException:
        return
    try:
        cleanup_task = asyncio.create_task(_wait_writer_closed_bounded(writer))
    except BaseException:
        return
    cleanup_task.add_done_callback(_consume_task_exception)


async def _wait_writer_closed_bounded(writer: Any) -> None:
    """以 bounded task wait 觀察 late writer 的 wait_closed，避免 cleanup task 遺留例外。"""
    try:
        wait_task = asyncio.create_task(_wait_writer_closed(writer))
    except BaseException:
        return
    try:
        done, pending = await asyncio.wait(
            {wait_task},
            timeout=_RELAY_CLEANUP_TIMEOUT_SECONDS,
        )
    except BaseException:
        _observe_tasks({wait_task})
        raise
    _observe_tasks(done | pending)


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Copy bounded chunks from one stream to another."""
    while chunk := await reader.read(64 * 1024):
        writer.write(chunk)
        await writer.drain()
