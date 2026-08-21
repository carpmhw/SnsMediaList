"""Tests for bounded thumbnail input validation and FFmpeg command construction."""

import asyncio
import gc
from typing import Any

import pytest

from sns_media_list.errors import AppError
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.services.thumbnail import (
    ThumbnailGenerator,
    build_ffmpeg_command,
    validate_generated_jpeg,
    validate_thumbnail_input,
)

VALID_JPEG = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xd9"


class Reader:
    """Provide deterministic upstream body reads for thumbnail tests."""

    def __init__(self, body: bytes) -> None:
        """Store the body to read."""
        self.body = body

    async def read(self, size: int) -> bytes:
        """Return one bounded body chunk."""
        data, self.body = self.body[:size], self.body[size:]
        return data

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return an unused chunk trailer."""
        return b"\r\n"

    async def readexactly(self, size: int) -> bytes:
        """Return one exact protocol section."""
        data, self.body = self.body[:size], self.body[size:]
        return data


class FragmentedReader(Reader):
    """Return one byte per read to simulate fragmented transport chunks."""

    async def read(self, size: int) -> bytes:
        """Return one byte regardless of the requested chunk size."""
        return await super().read(1)


class Writer:
    """Provide no-op upstream cleanup for thumbnail tests."""

    def close(self) -> None:
        """Close the fake upstream connection."""

    async def wait_closed(self) -> None:
        """Complete fake upstream cleanup."""


class Pipe:
    """Capture bytes written to a fake subprocess stdin."""

    def __init__(self, *, broken: bool = False) -> None:
        """Initialize an empty pipe."""
        self.body = bytearray()
        self.closed = False
        self.broken = broken

    def write(self, data: bytes) -> None:
        """Append one input chunk."""
        self.body.extend(data)

    async def drain(self) -> None:
        """Complete a fake pipe drain."""
        if self.broken:
            raise BrokenPipeError

    def close(self) -> None:
        """Close the fake stdin pipe."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Complete fake stdin cleanup."""


class Readable:
    """Return one bounded subprocess output stream."""

    def __init__(self, body: bytes) -> None:
        """Store output bytes."""
        self.body = body
        self.max_read_size = 0

    async def read(self, size: int) -> bytes:
        """Return the next output chunk."""
        self.max_read_size = max(self.max_read_size, size)
        data, self.body = self.body[:size], self.body[size:]
        return data


class BlockingReadable(Readable):
    """Track cancellation of a subprocess stream that never reaches EOF."""

    def __init__(self) -> None:
        """Initialize the cancellation signal."""
        super().__init__(b"")
        self.started = asyncio.Event()
        self.cancelled = False

    async def read(self, size: int) -> bytes:
        """Wait indefinitely until the owning task is cancelled."""
        del size
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""


class RawTimeoutReadable(Readable):
    """模擬 FFmpeg child IO 自己拋出的 raw TimeoutError。"""

    async def read(self, size: int) -> bytes:
        """拋出不應被誤判為 generation timeout 的 child IO 例外。"""
        _ = size
        raise TimeoutError("private child IO timeout")


class CancellationResistantOperation:
    """模擬取消後仍等待外部訊號，完成時拋出例外的 awaitable。"""

    def __init__(self) -> None:
        """建立操作狀態同步事件。"""
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        """忽略取消直到收到釋放訊號，再拋出受控例外。"""
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        self.finished.set()
        raise RuntimeError("detached cleanup failure")


class CancellationResistantReadable(Readable):
    """以 cancellation-resistant operation 模擬不會及時結束的 stdout。"""

    def __init__(self, operation: CancellationResistantOperation) -> None:
        """保存 stdout 的受控延遲操作。"""
        super().__init__(b"")
        self.operation = operation

    async def read(self, size: int) -> bytes:
        """等待受控操作完成並拋出其最終例外。"""
        del size
        await self.operation.run()
        return b""


class CancellationResistantPipe(Pipe):
    """以 cancellation-resistant operation 模擬延遲的 stdin close。"""

    def __init__(self, operation: CancellationResistantOperation) -> None:
        """建立帶有受控 wait_closed 的 stdin。"""
        super().__init__()
        self.operation = operation

    async def wait_closed(self) -> None:
        """等待受控操作完成並拋出其最終例外。"""
        await self.operation.run()


class Process:
    """Provide a controllable FFmpeg subprocess double."""

    def __init__(
        self, output: bytes, *, returncode: int | None = 0, broken_pipe: bool = False
    ) -> None:
        """Store output and exit state."""
        self.stdin = Pipe(broken=broken_pipe)
        self.stdout = Readable(output)
        self.stderr = Readable(b"private ffmpeg details")
        self.exit_code = returncode
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        """Return the configured process result."""
        self.returncode = self.exit_code
        return self.exit_code or 0

    def terminate(self) -> None:
        """Record graceful process termination."""
        self.terminated = True

    def kill(self) -> None:
        """Record forced process termination."""
        self.killed = True


class TerminateOSErrorProcess(Process):
    """模擬 process.terminate 拋出的 raw OS 例外。"""

    def terminate(self) -> None:
        """拋出不應覆蓋 generation primary error 的 OS 例外。"""
        raise OSError("private terminate detail")


class TerminateOSErrorAliveProcess(Process):
    """模擬 terminate 失敗且 process 仍存活的 FFmpeg process。"""

    def terminate(self) -> None:
        """拋出 terminate OS 例外並保留 process 存活。"""
        raise OSError("private terminate detail")

    async def wait(self) -> int:
        """process 存活時等待，收到 kill 後立即返回。"""
        if not self.killed:
            await asyncio.sleep(1)
        return -9


class KillOSErrorProcess(Process):
    """模擬 process.kill 拋出的 raw OS 例外。"""

    async def wait(self) -> int:
        """持續等待以迫使 stop process 進入 kill 分支。"""
        await asyncio.sleep(1)
        return -9

    def kill(self) -> None:
        """拋出不應穿透 bounded process cleanup 的 OS 例外。"""
        raise OSError("private kill detail")


class CancellationResistantStopProcess:
    """模擬第一次 wait 被取消後仍存活，且 kill 後才可完成的 process。"""

    def __init__(self) -> None:
        """初始化 process cleanup 的同步事件。"""
        self.terminated = False
        self.killed = False
        self.wait_started = asyncio.Event()
        self.wait_cancelled = asyncio.Event()
        self.kill_called = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.wait_finished = asyncio.Event()

    def terminate(self) -> None:
        """記錄 terminate 請求但不讓 process 結束。"""
        self.terminated = True

    def kill(self) -> None:
        """記錄 kill 請求並通知測試 cleanup 已升級。"""
        self.killed = True
        self.kill_called.set()

    async def wait(self) -> int:
        """吞掉 wait task cancellation，直到測試釋放後拋出受控例外。"""
        self.wait_started.set()
        try:
            await self.release_wait.wait()
        except asyncio.CancelledError:
            self.wait_cancelled.set()
            await self.release_wait.wait()
        finally:
            self.wait_finished.set()
        raise RuntimeError("detached process wait failure")


class SlowCloseResponse(MediaResponse):
    """Delay source cleanup beyond the thumbnail generation deadline."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化可追蹤 close ownership 的 upstream response。"""
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    async def close(self) -> None:
        """Wait until generation cleanup is cancelled."""
        self.close_calls += 1
        await asyncio.sleep(1)


def make_response(
    body: bytes,
    content_type: str = "image/jpeg",
    *,
    reader: Reader | None = None,
) -> MediaResponse:
    """Construct a bounded fake upstream media response."""
    return MediaResponse(
        200,
        {"content-type": content_type, "content-length": str(len(body))},
        reader or Reader(body),
        Writer(),
        max_bytes=100_000,
    )


@pytest.mark.parametrize(
    ("content_type", "prefix"),
    [
        ("image/jpeg", b"\xff\xd8\xff\xe0jpeg"),
        ("image/png", b"\x89PNG\r\n\x1a\npng"),
        ("image/gif", b"GIF89agif"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBPwebp"),
        ("video/mp4", b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"),
        ("video/webm", b"\x1a\x45\xdf\xa3webm"),
    ],
)
def test_validate_thumbnail_input_accepts_supported_signatures(
    content_type: str, prefix: bytes
) -> None:
    """Verify supported raster and progressive video signatures are accepted."""
    assert validate_thumbnail_input(content_type, prefix) == content_type


@pytest.mark.parametrize(
    ("content_type", "prefix"),
    [
        ("image/jpeg", b"<html>"),
        ("image/svg+xml", b"<svg>"),
        ("text/html", b"<html>"),
        ("application/json", b"{}"),
        ("video/mp4", b"#EXTM3U"),
        ("image/png", b"\xff\xd8\xffjpeg"),
        ("video/webm", b"\x00\x00\x00\x00webm"),
    ],
)
def test_validate_thumbnail_input_rejects_unsupported_or_mislabeled_content(
    content_type: str, prefix: bytes
) -> None:
    """Verify active content, manifests, and mismatched signatures fail closed."""
    with pytest.raises(AppError) as exc_info:
        validate_thumbnail_input(content_type, prefix)

    assert exc_info.value.code == "upstream_media_invalid"


def test_validate_thumbnail_input_rejects_html_containing_mp4_brand_text() -> None:
    """Verify arbitrary ftyp text cannot satisfy the MP4 signature check."""
    with pytest.raises(AppError) as exc_info:
        validate_thumbnail_input("video/mp4", b"<html>ftyp-not-a-container</html>")

    assert exc_info.value.code == "upstream_media_invalid"


def test_validate_thumbnail_input_rejects_non_container_ftyp_prefix() -> None:
    """Verify an HTML-like prefix at the MP4 box offset is not enough by itself."""
    with pytest.raises(AppError) as exc_info:
        validate_thumbnail_input("video/mp4", b"\x00\x00\x00\x10ftyp<svg\x00\x00\x00\x00")

    assert exc_info.value.code == "upstream_media_invalid"


def test_ffmpeg_command_is_fixed_and_pipe_only() -> None:
    """Verify FFmpeg receives fixed pipe arguments and no caller-controlled URL."""
    command = build_ffmpeg_command(max_edge=640)

    assert command[0] == "ffmpeg"
    assert "-nostdin" in command
    assert "pipe:0" in command
    assert "pipe:1" in command
    assert "-threads" in command
    assert "1" in command
    assert "-frames:v" in command
    assert "1" in command
    assert "http" not in " ".join(command)
    assert "Cookie" not in " ".join(command)


@pytest.mark.asyncio
async def test_thumbnail_generator_streams_input_and_returns_jpeg(monkeypatch) -> None:
    """Verify a valid source is piped to FFmpeg and its JPEG output is returned."""
    process = Process(VALID_JPEG)

    async def fake_create(*_args, **_kwargs):
        """Return the controlled FFmpeg process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    source = b"\xff\xd8\xff\xe0source-image\xff\xd9"

    output = await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
        make_response(source)
    )

    assert output.startswith(b"\xff\xd8\xff")
    assert bytes(process.stdin.body) == source
    assert process.stdin.closed is True


@pytest.mark.asyncio
async def test_thumbnail_generator_leaves_source_cleanup_to_caller(monkeypatch) -> None:
    """縮圖 generator 不得 close source，source cleanup 應由 route owner 負責。"""
    process = Process(VALID_JPEG)

    async def fake_create(*_args, **_kwargs):
        """回傳受控的成功 FFmpeg process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    source_body = b"\xff\xd8\xff\xe0source-image\xff\xd9"
    source = SlowCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(source_body))},
        Reader(source_body),
        Writer(),
        max_bytes=100,
    )

    output = await ThumbnailGenerator(
        input_bytes=100,
        output_bytes=100,
        timeout_seconds=0.01,
    ).generate(source)

    assert output == VALID_JPEG
    assert source.close_calls == 0


@pytest.mark.asyncio
async def test_thumbnail_generator_accepts_fragmented_signature(monkeypatch) -> None:
    """Verify input signatures are collected across fragmented response chunks."""
    process = Process(VALID_JPEG)

    async def fake_create(*_args, **_kwargs):
        """Return the controlled FFmpeg process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    source = b"\x89PNG\r\n\x1a\nfragmented-png"

    output = await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
        make_response(source, content_type="image/png", reader=FragmentedReader(source))
    )

    assert output == VALID_JPEG


@pytest.mark.asyncio
async def test_thumbnail_generator_rejects_oversized_output_and_terminates(monkeypatch) -> None:
    """Verify output limits stop FFmpeg without returning partial JPEG bytes."""
    process = Process(b"\xff\xd8\xff" + b"x" * 101)

    async def fake_create(*_args, **_kwargs):
        """Return the controlled oversized-output process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert process.terminated is True
    assert process.stdout.max_read_size <= 101


@pytest.mark.asyncio
async def test_thumbnail_generator_rejects_invalid_input_before_spawning(monkeypatch) -> None:
    """Verify mislabeled content is rejected before FFmpeg starts."""
    spawned = False

    async def fake_create(*_args, **_kwargs):
        """Record an unexpected process launch."""
        nonlocal spawned
        spawned = True
        return Process(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"<html>not-image</html>")
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert exc_info.value.deterministic is True
    assert spawned is False


@pytest.mark.asyncio
async def test_thumbnail_generator_maps_nonzero_exit_and_hides_stderr(monkeypatch) -> None:
    """Verify FFmpeg diagnostics become a stable error without leaking stderr."""
    process = Process(VALID_JPEG, returncode=1)

    async def fake_create(*_args, **_kwargs):
        """Return a failed FFmpeg process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert "private ffmpeg details" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_thumbnail_generator_accepts_early_ffmpeg_stdin_close(monkeypatch) -> None:
    """Verify a successful one-frame FFmpeg exit can close stdin before input EOF."""
    process = Process(VALID_JPEG, broken_pipe=True)

    async def fake_create(*_args, **_kwargs):
        """Return a process that stops reading after its first frame."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    output = await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
        make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
    )

    assert output == VALID_JPEG


@pytest.mark.asyncio
async def test_thumbnail_generator_ignores_closed_stdin_handler(monkeypatch) -> None:
    """Verify uvloop's already-closed stdin handler does not fail a valid thumbnail."""
    process = Process(VALID_JPEG)

    async def wait_closed_with_closed_handler() -> None:
        """Raise the uvloop cleanup error for an already-closed subprocess pipe."""
        raise RuntimeError("unable to perform operation; the handler is closed")

    def write_with_closed_handler(_data: bytes) -> None:
        """Raise the uvloop error when FFmpeg closes stdin before input EOF."""
        raise RuntimeError("unable to perform operation; the handler is closed")

    process.stdin.write = write_with_closed_handler  # type: ignore[method-assign]
    process.stdin.wait_closed = wait_closed_with_closed_handler  # type: ignore[method-assign]

    async def fake_create(*_args, **_kwargs):
        """Return a process whose stdin handler closed before cleanup completed."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    output = await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
        make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
    )

    assert output == VALID_JPEG


@pytest.mark.asyncio
async def test_thumbnail_generator_cancels_sibling_io_tasks_on_feed_failure(monkeypatch) -> None:
    """Verify feed failure cancels and awaits FFmpeg output readers before returning."""
    process = Process(VALID_JPEG)
    process.stdout = BlockingReadable()

    async def failing_drain() -> None:
        """Raise an unrelated feed failure after the first write."""
        raise RuntimeError("feed failed")

    process.stdin.drain = failing_drain  # type: ignore[method-assign]

    async def fake_create(*_args, **_kwargs):
        """Return a process whose output reader must be cleaned up."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError):
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert process.stdout.started.is_set()  # type: ignore[union-attr]
    assert process.stdout.cancelled is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_thumbnail_generator_maps_timeout_and_terminates(monkeypatch) -> None:
    """Verify a stalled FFmpeg process is terminated at the configured deadline."""
    process = Process(VALID_JPEG)

    async def wait_forever() -> int:
        """Keep the fake process alive beyond the deadline."""
        while not process.terminated and not process.killed:
            await asyncio.sleep(0.01)
        return -15

    process.wait = wait_forever  # type: ignore[method-assign]

    async def fake_create(*_args, **_kwargs):
        """Return a stalled FFmpeg process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100, timeout_seconds=0.01).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert process.terminated is True


@pytest.mark.asyncio
async def test_thumbnail_generator_maps_child_raw_timeout_to_safe_failure(monkeypatch) -> None:
    """child IO 自己拋出的 raw timeout 應映射為 generic thumbnail failure。"""
    process = Process(VALID_JPEG)
    process.stdout = RawTimeoutReadable(b"")

    async def fake_create(*_args, **_kwargs):
        """回傳 child stdout 自己拋出 timeout 的受控 process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert exc_info.value.message == "Thumbnail generation failed."
    assert "timed out" not in exc_info.value.message
    assert "private child IO timeout" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_thumbnail_generator_bounds_uncooperative_process_cleanup(monkeypatch) -> None:
    """Verify a process that ignores terminate and kill cannot block cleanup forever."""
    process = Process(VALID_JPEG)

    async def wait_forever() -> int:
        """Ignore all termination attempts."""
        await asyncio.Event().wait()
        return -9

    process.wait = wait_forever  # type: ignore[method-assign]

    async def fake_create(*_args, **_kwargs):
        """Return an uncooperative FFmpeg process."""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await asyncio.wait_for(
            ThumbnailGenerator(input_bytes=100, output_bytes=100, timeout_seconds=0.01).generate(
                make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
            ),
            timeout=1.5,
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.parametrize("process_type", [TerminateOSErrorProcess, KillOSErrorProcess])
@pytest.mark.asyncio
async def test_thumbnail_stop_process_absorbs_raw_os_errors(process_type: type[Process]) -> None:
    """terminate 或 kill 的 raw OS 例外不得穿透 bounded process cleanup。"""
    process = process_type(VALID_JPEG)

    await ThumbnailGenerator(timeout_seconds=0.01)._stop_process(process)


@pytest.mark.asyncio
async def test_thumbnail_stop_process_kills_when_terminate_fails_and_process_lives() -> None:
    """terminate 失敗且 process 仍存活時應繼續 fallback 到 kill。"""
    process = TerminateOSErrorAliveProcess(VALID_JPEG)

    await ThumbnailGenerator(timeout_seconds=0.01)._stop_process(process)

    assert process.killed is True


@pytest.mark.asyncio
async def test_thumbnail_stop_process_kills_after_first_wait_cancellation() -> None:
    """第一次 wait 遇 caller cancellation 時仍須 kill 並重拋取消。"""
    process = CancellationResistantStopProcess()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 wait task observer 消費的 detached 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    stop_task = asyncio.create_task(ThumbnailGenerator(timeout_seconds=0.01)._stop_process(process))
    try:
        await asyncio.wait_for(process.wait_started.wait(), timeout=0.1)
        stop_task.cancel()
        await asyncio.wait_for(process.wait_cancelled.wait(), timeout=0.1)
        await asyncio.wait_for(process.kill_called.wait(), timeout=0.1)
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert process.killed is True
    finally:
        process.release_wait.set()
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await asyncio.wait_for(process.wait_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert unhandled == []
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_thumbnail_generator_preserves_primary_error_when_terminate_fails(
    monkeypatch,
) -> None:
    """process terminate 失敗時仍應保留原始 generated thumbnail error。"""
    process = TerminateOSErrorProcess(b"not-a-jpeg")

    async def fake_create(*_args, **_kwargs):
        """回傳輸出無效且 terminate 會失敗的 FFmpeg process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError, match="generated thumbnail is invalid"):
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )


@pytest.mark.parametrize("cleanup_kind", ["pipe", "process"])
@pytest.mark.asyncio
async def test_thumbnail_cleanup_detaches_cancellation_resistant_task(cleanup_kind: str) -> None:
    """縮圖清理 timeout 後應 detach task 並消費其最終例外。"""
    operation = CancellationResistantOperation()
    generator = ThumbnailGenerator(timeout_seconds=0.01)
    process = Process(VALID_JPEG)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 detached cleanup observer 消費的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        if cleanup_kind == "pipe":
            process.stdin = CancellationResistantPipe(operation)

            async def chunks() -> Any:
                """提供沒有額外 body 的空 async iterator。"""
                if False:
                    yield b""

            await generator._feed_process(process, b"source", chunks())
        else:
            process.wait = operation.run  # type: ignore[method-assign]
            assert await generator._wait_process(process, 0.01) is False

        await asyncio.wait_for(operation.cancelled.wait(), timeout=0.1)
        operation.release.set()
        await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        operation.release.set()
        if not operation.finished.is_set():
            await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_thumbnail_generation_detaches_cancelled_io_task(monkeypatch) -> None:
    """取消縮圖時，無法及時結束的 IO task 最終例外不得產生警告。"""
    operation = CancellationResistantOperation()
    process = Process(VALID_JPEG)
    process.stdout = CancellationResistantReadable(operation)

    async def fake_create(*_args, **_kwargs):
        """回傳 stdout 會延遲失敗的受控 process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 aggregate task observer 消費的例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    generation = asyncio.create_task(
        ThumbnailGenerator(input_bytes=100, output_bytes=100, timeout_seconds=0.01).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )
    )
    try:
        await operation.started.wait()
        generation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await generation
        await asyncio.wait_for(operation.cancelled.wait(), timeout=0.1)
        operation.release.set()
        await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        operation.release.set()
        if not operation.finished.is_set():
            await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        if not generation.done():
            generation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await generation
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_thumbnail_generation_timeout_detaches_cancellation_resistant_io(
    monkeypatch,
) -> None:
    """縮圖 timeout 時不得等待 cancellation-resistant IO task 無限完成。"""
    operation = CancellationResistantOperation()
    process = Process(VALID_JPEG)
    process.stdout = CancellationResistantReadable(operation)

    async def fake_create(*_args, **_kwargs):
        """回傳 stdout 會在 timeout 後延遲失敗的受控 process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    generation = asyncio.create_task(
        ThumbnailGenerator(input_bytes=100, output_bytes=100, timeout_seconds=0.01).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )
    )

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 timeout cleanup observer 消費的例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        await operation.started.wait()
        started = loop.time()
        done, _pending = await asyncio.wait({generation}, timeout=0.2)
        assert generation in done
        with pytest.raises(AppError) as exc_info:
            await generation
        assert exc_info.value.code == "upstream_media_invalid"
        assert loop.time() - started < 0.1
        await asyncio.wait_for(operation.cancelled.wait(), timeout=0.1)
        operation.release.set()
        await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        operation.release.set()
        if not operation.finished.is_set():
            await asyncio.wait_for(operation.finished.wait(), timeout=0.1)
        if not generation.done():
            try:
                await asyncio.wait_for(generation, timeout=0.2)
            except BaseException:
                pass
        if generation.done():
            try:
                generation.exception()
            except BaseException:
                pass
        loop.set_exception_handler(previous_handler)


def test_validate_generated_jpeg_requires_dimensions_within_edge() -> None:
    """Verify generated output contains a bounded JPEG frame dimension."""
    validate_generated_jpeg(VALID_JPEG, max_bytes=100, max_edge=640)

    oversized = b"\xff\xd8\xff\xc0\x00\x0b\x08\x03\x00\x01\x03\x01\x01\x11\x00\xff\xd9"
    with pytest.raises(AppError):
        validate_generated_jpeg(oversized, max_bytes=100, max_edge=640)


@pytest.mark.asyncio
async def test_thumbnail_generator_rejects_input_limit_before_process(monkeypatch) -> None:
    """Verify oversized source content is rejected before FFmpeg starts."""
    spawned = False

    async def fake_create(*_args, **_kwargs):
        """Record an unexpected process launch."""
        nonlocal spawned
        spawned = True
        return Process(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=10, output_bytes=100).generate(
            make_response(b"\xff\xd8\xff\xe0source\xff\xd9")
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert exc_info.value.deterministic is True
    assert spawned is False


@pytest.mark.asyncio
async def test_thumbnail_generator_maps_upstream_oserror_to_safe_error() -> None:
    """Verify transport exceptions are converted to the stable media error."""

    class FailingResponse:
        """Raise a transport error while reading source bytes."""

        headers = {"content-type": "image/jpeg"}

        def iter_bytes(self, **_kwargs):
            """Return an iterator that fails like an upstream socket."""

            async def chunks():
                """Raise one bounded transport failure."""
                raise OSError("private socket detail")
                yield b""

            return chunks()

        async def close(self) -> None:
            """Complete fake source cleanup."""

    with pytest.raises(AppError) as exc_info:
        await ThumbnailGenerator(input_bytes=100, output_bytes=100).generate(FailingResponse())  # type: ignore[arg-type]

    assert exc_info.value.code == "upstream_media_invalid"
    assert "private socket detail" not in str(exc_info.value)
