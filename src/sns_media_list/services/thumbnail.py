"""Bounded, offline FFmpeg thumbnail generation helpers."""

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..errors import AppError

if TYPE_CHECKING:
    from ..network.media_client import MediaResponse

_SUPPORTED_INPUTS = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "video/mp4": (b"ftyp",),
    "video/webm": (b"\x1a\x45\xdf\xa3",),
}


def validate_thumbnail_input(content_type: str, prefix: bytes) -> str:
    """Validate a supported content type against its bounded input signature."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    signatures = _SUPPORTED_INPUTS.get(normalized)
    if signatures is None or not _signature_matches(normalized, signatures, prefix):
        raise AppError("upstream_media_invalid", "The thumbnail input is not supported.")
    return normalized


def validate_thumbnail_media_class(content_type: str, media_class: str) -> None:
    """Reject a generated source whose MIME class differs from its token."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    if media_class == "image" and not normalized.startswith("image/"):
        raise AppError("upstream_media_invalid", "The thumbnail media class is invalid.")
    if media_class == "video" and not normalized.startswith("video/"):
        raise AppError("upstream_media_invalid", "The thumbnail media class is invalid.")


def build_ffmpeg_command(*, max_edge: int) -> list[str]:
    """Build the fixed offline FFmpeg command used for one thumbnail frame."""
    scale = (
        f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)':force_original_aspect_ratio=decrease"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "pipe",
        "-threads",
        "1",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "1000000",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-vf",
        scale,
        "-map_metadata",
        "-1",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "-q:v",
        "5",
        "pipe:1",
    ]


def build_ffmpeg_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal FFmpeg environment without inherited proxy or secret variables."""
    source = base or {}
    return {
        "PATH": source.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": source.get("LANG", "C.UTF-8"),
        "LC_ALL": source.get("LC_ALL", "C.UTF-8"),
        "HOME": "/tmp",
        "NO_COLOR": "1",
    }


class ThumbnailGenerator:
    """Generate one validated JPEG through a bounded offline FFmpeg process."""

    def __init__(
        self,
        *,
        input_bytes: int = 32_000_000,
        output_bytes: int = 1_000_000,
        timeout_seconds: float = 10.0,
        max_edge: int = 640,
    ) -> None:
        """Store fixed byte, timeout, and output-dimension limits."""
        self.input_bytes = input_bytes
        self.output_bytes = output_bytes
        self.timeout_seconds = timeout_seconds
        self.max_edge = max_edge

    async def generate(self, response: "MediaResponse") -> bytes:
        """Stream one validated upstream response through FFmpeg and return JPEG bytes."""
        process: Any | None = None
        try:
            content_type = response.headers.get("content-type", "")
            iterator = response.iter_bytes(chunk_size=512, max_bytes=self.input_bytes)
            first_chunk = await anext(iterator)
            prefix = bytearray(first_chunk)
            while len(prefix) < 512:
                try:
                    prefix.extend(await anext(iterator))
                except StopAsyncIteration:
                    break
            try:
                validate_thumbnail_input(content_type, bytes(prefix[:512]))
            except AppError as error:
                raise _thumbnail_error(error.message, deterministic=True) from error
            process = await asyncio.create_subprocess_exec(
                *build_ffmpeg_command(max_edge=self.max_edge),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_ffmpeg_environment(),
                start_new_session=True,
            )
            tasks = [
                asyncio.create_task(self._feed_process(process, bytes(prefix), iterator)),
                asyncio.create_task(self._collect_output(process)),
                asyncio.create_task(self._discard_stderr(process)),
                asyncio.create_task(process.wait()),
            ]
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=self.timeout_seconds,
                )
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                done, _pending = await asyncio.wait(tasks, timeout=min(self.timeout_seconds, 1.0))
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                raise
            if results[3] != 0:
                raise _thumbnail_error("Thumbnail generation failed.", deterministic=True)
            output = results[1]
            if not isinstance(output, bytes):
                raise _thumbnail_error("Thumbnail generation failed.", deterministic=True)
            try:
                validate_generated_jpeg(
                    output,
                    max_bytes=self.output_bytes,
                    max_edge=self.max_edge,
                )
            except AppError as error:
                raise _thumbnail_error(error.message, deterministic=True) from error
            return output
        except StopAsyncIteration as error:
            raise _thumbnail_error(
                "The thumbnail input body was empty.", deterministic=True
            ) from error
        except AppError as error:
            if "size limit" in error.message:
                error.deterministic = True
            raise
        except TimeoutError as error:
            raise _thumbnail_error(
                "Thumbnail generation timed out.", deterministic=False
            ) from error
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            raise AppError("upstream_media_invalid", "Thumbnail generation failed.") from error
        finally:
            if process is not None:
                await self._stop_process(process)
            await self._close_response(response)

    async def _close_response(self, response: "MediaResponse") -> None:
        """Close the source within the generated-preview deadline."""
        task = asyncio.create_task(response.close())
        done, _pending = await asyncio.wait({task}, timeout=min(self.timeout_seconds, 1.0))
        if done:
            await asyncio.gather(task, return_exceptions=True)
        else:
            task.cancel()

    async def _feed_process(self, process: Any, first_chunk: bytes, iterator: Any) -> None:
        """Feed bounded upstream chunks to FFmpeg stdin and close it reliably."""
        stdin = process.stdin
        if stdin is None:
            raise AppError("upstream_media_invalid", "Thumbnail process input is unavailable.")
        try:
            try:
                stdin.write(first_chunk)
                await stdin.drain()
                async for chunk in iterator:
                    stdin.write(chunk)
                    await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return
            except RuntimeError as error:
                if "handler is closed" not in str(error):
                    raise
                return
        finally:
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if wait_closed is not None:
                task = asyncio.create_task(wait_closed())
                done, _pending = await asyncio.wait({task}, timeout=min(self.timeout_seconds, 1.0))
                if done:
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    task.cancel()

    async def _collect_output(self, process: Any) -> bytes:
        """Read FFmpeg stdout while rejecting output larger than the configured bound."""
        stdout = process.stdout
        if stdout is None:
            raise AppError("upstream_media_invalid", "Thumbnail process output is unavailable.")
        output = bytearray()
        while True:
            remaining = self.output_bytes - len(output)
            chunk = await stdout.read(min(64 * 1024, remaining + 1))
            if not chunk:
                return bytes(output)
            if len(chunk) > remaining:
                raise _thumbnail_error(
                    "The generated thumbnail exceeds its limit.", deterministic=True
                )
            output.extend(chunk)

    async def _discard_stderr(self, process: Any) -> None:
        """Drain FFmpeg diagnostics without retaining or exposing them."""
        stderr = process.stderr
        if stderr is None:
            return
        while await stderr.read(4096):
            continue

    async def _stop_process(self, process: Any) -> None:
        """Terminate FFmpeg and escalate to kill if it does not exit promptly."""
        cleanup_timeout = min(self.timeout_seconds, 1.0)
        try:
            process.terminate()
        except ProcessLookupError:
            return
        if await self._wait_process(process, cleanup_timeout):
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        await self._wait_process(process, cleanup_timeout)

    async def _wait_process(self, process: Any, timeout: float) -> bool:
        """Wait for a subprocess with a hard cleanup bound."""
        task = asyncio.create_task(process.wait())
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if done:
            await asyncio.gather(task, return_exceptions=True)
            return True
        task.cancel()
        return False


def validate_generated_jpeg(data: bytes, *, max_bytes: int, max_edge: int) -> None:
    """Validate bounded JPEG framing before generated bytes reach a browser or cache."""
    if len(data) > max_bytes or len(data) < 5 or not data.startswith(b"\xff\xd8\xff"):
        raise AppError("upstream_media_invalid", "The generated thumbnail is invalid.")
    if not data.endswith(b"\xff\xd9"):
        raise AppError("upstream_media_invalid", "The generated thumbnail is invalid.")
    dimensions = _jpeg_dimensions(data)
    if dimensions is None or min(dimensions) <= 0 or max(dimensions) > max_edge:
        raise AppError("upstream_media_invalid", "The generated thumbnail dimensions are invalid.")


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width and height from a bounded JPEG frame header."""
    index = 2
    sof_markers = (
        set(range(0xC0, 0xC4))
        | set(range(0xC5, 0xC8))
        | set(range(0xC9, 0xCC))
        | set(range(0xCD, 0xD0))
    )
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += length
    return None


def _thumbnail_error(message: str, *, deterministic: bool) -> AppError:
    """Build a safe thumbnail error with an internal cache classification."""
    error = AppError("upstream_media_invalid", message)
    error.deterministic = deterministic
    return error


def _signature_matches(content_type: str, signatures: tuple[bytes, ...], prefix: bytes) -> bool:
    """Match a signature at the format-specific location in a bounded prefix."""
    if content_type == "image/webp":
        return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if content_type == "video/mp4":
        major_brand = prefix[8:12]
        return (
            len(prefix) >= 16
            and 16 <= int.from_bytes(prefix[:4], "big") <= len(prefix)
            and int.from_bytes(prefix[:4], "big") % 4 == 0
            and prefix[4:8] == b"ftyp"
            and len(major_brand) == 4
            and all(
                byte in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                for byte in major_brand
            )
        )
    return any(prefix.startswith(signature) for signature in signatures)
