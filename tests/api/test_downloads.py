"""Tests for token-bound preview and attachment routes."""

import asyncio
import ipaddress
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from sns_media_list.api.limits import Lease, RequestLimiter
from sns_media_list.api.routes import (
    DeadlineStreamingResponse,
    _await_with_deadline,
    _close_response_with_timeout,
    _generate_preview,
    _stream_media,
)
from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.models import PrivateMediaRecord
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.security.tokens import MediaTokenDraft, TokenStore
from sns_media_list.services.extraction_service import ExtractionService
from sns_media_list.services.thumbnail import ThumbnailGenerator
from sns_media_list.services.thumbnail_cache import ThumbnailCache, ThumbnailCoordinator

_USE_FAKE_MEDIA_CLIENT = object()


class FakeExtractor:
    """Return one image with a preview token."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return deterministic media metadata."""
        return [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "image",
                "url": "https://pbs.twimg.com/media/1.jpg?name=orig",
                "preview_url": "https://pbs.twimg.com/media/1.jpg?name=small",
                "extension": "jpg",
                "width": 100,
                "height": 100,
                "progressive": True,
            }
        ]


class StoryImageExtractor:
    """Return one exact Instagram Story image without authentication metadata."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return deterministic Story media with a trusted Instagram preview."""
        return [
            {
                "platform": "instagram",
                "post_url": ("https://www.instagram.com/stories/example.user/1111111111111111111/"),
                "post_id": "1111111111111111111",
                "num": 1,
                "type": "image",
                "url": "https://scontent.cdninstagram.com/story-image.jpg?source=private",
                "preview_url": (
                    "https://scontent.cdninstagram.com/story-image-preview.jpg?source=private"
                ),
                "extension": "jpg",
                "width": 1080,
                "height": 1920,
                "progressive": True,
            }
        ]


class GeneratedExtractor:
    """Return one video without a platform poster for generated-preview tests."""

    async def extract(self, _post_url: Any) -> list[dict[str, Any]]:
        """Return deterministic media metadata without preview metadata."""
        return [
            {
                "platform": "x",
                "post_url": "https://x.com/creator/status/1",
                "post_id": "1",
                "num": 1,
                "type": "video",
                "url": "https://video.twimg.com/1.mp4",
                "extension": "mp4",
                "width": 100,
                "height": 100,
                "progressive": True,
            }
        ]


class FakeThumbnailGenerator:
    """Return one bounded JPEG while counting generation calls."""

    def __init__(self) -> None:
        """Initialize the generation counter."""
        self.calls = 0

    async def generate(self, response: MediaResponse) -> bytes:
        """回傳 deterministic JPEG，並將 source cleanup 留給 route owner。"""
        self.calls += 1
        _ = response
        return b"\xff\xd8\xff\xe0generated\xff\xd9"


class FailingThumbnailGenerator:
    """Return one deterministic generation failure for negative-cache tests."""

    def __init__(self) -> None:
        """Initialize the failure counter."""
        self.calls = 0

    async def generate(self, response: MediaResponse) -> bytes:
        """拋出 safe deterministic error，並將 source cleanup 留給 route owner。"""
        self.calls += 1
        _ = response
        error = AppError("upstream_media_invalid", "safe thumbnail failure")
        error.deterministic = True
        raise error


class PrimaryFailingThumbnailGenerator:
    """Raise a primary thumbnail error without closing the source response."""

    async def generate(self, _response: MediaResponse) -> bytes:
        """Raise the primary error used to verify cleanup error precedence."""
        raise AppError("upstream_media_invalid", "primary thumbnail failure")


class BlockingThumbnailGenerator(FakeThumbnailGenerator):
    """Hold one generated preview open so a different token reaches saturation."""

    timeout_seconds = 1.0

    def __init__(self) -> None:
        """Initialize synchronization events for the concurrent endpoint test."""
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, response: MediaResponse) -> bytes:
        """等待測試發出第二個 generated-preview request，不負責 source cleanup。"""
        self.calls += 1
        self.started.set()
        _ = response
        await self.release.wait()
        return b"\xff\xd8\xff\xe0generated\xff\xd9"


class TimedThumbnailGenerator(FakeThumbnailGenerator):
    """Expose a short generation deadline for fetch-timeout tests."""

    timeout_seconds = 0.01


class SlowMediaClient:
    """Delay source response headers beyond the generated preview deadline."""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Sleep until the route-level generation timeout cancels the fetch."""
        await asyncio.sleep(1)
        raise AssertionError("fetch should have been cancelled")


class CancellationResistantFetchMediaClient:
    """模擬取消後仍等待釋放訊號的 upstream fetch。"""

    def __init__(self) -> None:
        """初始化 fetch operation 的 lifecycle 事件。"""
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """忽略 cancellation 直到釋放，再拋出受控背景例外。"""
        _ = headers
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        finally:
            self.finished.set()
        raise RuntimeError("private detached fetch failure")


class SuccessfulCancellationResistantFetchMediaClient:
    """模擬取消後仍成功回傳 response 的 upstream fetch。"""

    def __init__(self, response: MediaResponse) -> None:
        """保存 fetch 最終會回傳的 response 與生命週期事件。"""
        self.response = response
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """吞掉取消直到釋放，再回傳仍需由 route 關閉的 response。"""
        _ = headers
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        return self.response


class Reader:
    """Provide a fixed image body to the fake media client."""

    def __init__(self, body: bytes) -> None:
        """Store the response body."""
        self.body = body

    async def read(self, size: int) -> bytes:
        """Return the next body chunk."""
        data, self.body = self.body[:size], self.body[size:]
        return data

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return an empty trailer."""
        return b"\r\n"

    async def readexactly(self, size: int) -> bytes:
        """Return the requested bytes."""
        data, self.body = self.body[:size], self.body[size:]
        return data


class SequencedReader(Reader):
    """依序回傳可延遲或失敗的上游 body read。"""

    def __init__(self, steps: list[tuple[float, bytes | BaseException]]) -> None:
        """保存每次讀取的延遲與結果。"""
        super().__init__(b"")
        self.steps = steps
        self.read_calls = 0

    async def read(self, size: int) -> bytes:
        """執行下一個受控讀取步驟。"""
        self.read_calls += 1
        if not self.steps:
            return b""
        delay, result = self.steps.pop(0)
        if delay:
            await asyncio.sleep(delay)
        if isinstance(result, BaseException):
            raise result
        assert len(result) <= size
        return result


class TransportReader(SequencedReader):
    """提供真實 MediaClient transport 所需的 headers 與分段 body。"""

    def __init__(
        self,
        header_block: bytes,
        steps: list[tuple[float, bytes | BaseException]],
    ) -> None:
        """保存 raw response headers 與後續 body read 步驟。"""
        super().__init__(steps)
        self.header_block = header_block

    async def readuntil(self, _separator: bytes) -> bytes:
        """回傳受控 socket 的 raw response headers。"""
        return self.header_block


class BlockingSecondReadReader(Reader):
    """第一段立即回傳，第二段阻塞直到 stream task 被取消。"""

    def __init__(self) -> None:
        """初始化 read 與 cancellation 同步事件。"""
        super().__init__(b"")
        self.read_calls = 0
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._never = asyncio.Event()

    async def read(self, size: int) -> bytes:
        """在第二次 read 等待，並記錄 client disconnect 所造成的取消。"""
        _ = size
        self.read_calls += 1
        if self.read_calls == 1:
            return b"ab"
        self.blocked.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocked read should only finish by cancellation")


class ChunkedReader(Reader):
    """解析測試用的 HTTP chunk framing。"""

    async def readuntil(self, separator: bytes) -> bytes:
        """消耗並回傳下一段含分隔符號的 framing。"""
        index = self.body.find(separator)
        if index < 0:
            raise asyncio.IncompleteReadError(self.body, None)
        end = index + len(separator)
        data, self.body = self.body[:end], self.body[end:]
        return data


class Writer:
    """Provide no-op cleanup for fake upstream responses."""

    def close(self) -> None:
        """Close the fake writer."""

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


class ThumbnailStdin:
    """提供受控 FFmpeg stdin 的 bounded pipe double。"""

    def __init__(self) -> None:
        """初始化 stdin buffer 與 close 狀態。"""
        self.body = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        """記錄 generator 寫入的 source bytes。"""
        self.body.extend(value)

    async def drain(self) -> None:
        """完成受控 stdin drain。"""

    def close(self) -> None:
        """關閉受控 stdin pipe。"""
        self.closed = True

    async def wait_closed(self) -> None:
        """完成受控 stdin cleanup。"""


class ThumbnailProcess:
    """提供可控制 terminate/kill 行為的 FFmpeg process double。"""

    def __init__(
        self,
        output: bytes,
        *,
        fail_terminate: bool = False,
        fail_kill: bool = False,
        wait_forever: bool = False,
    ) -> None:
        """保存 FFmpeg output 與 process cleanup failure 設定。"""
        self.stdin = ThumbnailStdin()
        self.stdout = Reader(output)
        self.stderr = Reader(b"")
        self.fail_terminate = fail_terminate
        self.fail_kill = fail_kill
        self.wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        """回傳 process exit code，或保持執行以觸發 timeout cleanup。"""
        if self.wait_forever:
            await asyncio.sleep(1)
        return 0

    def terminate(self) -> None:
        """執行受控 terminate，必要時拋出 raw OS 例外。"""
        self.terminated = True
        if self.fail_terminate:
            raise OSError("private terminate detail")

    def kill(self) -> None:
        """執行受控 kill，必要時拋出 raw OS 例外。"""
        self.killed = True
        if self.fail_kill:
            raise OSError("private kill detail")


class CountingWriter(Writer):
    """記錄 upstream connection 被關閉的次數。"""

    def __init__(self) -> None:
        """初始化關閉次數。"""
        self.close_calls = 0
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        """記錄真正 MediaClient 寫入 socket 的 request bytes。"""
        self.writes.append(value)

    async def drain(self) -> None:
        """完成受控 socket request write。"""

    def close(self) -> None:
        """記錄一次 upstream connection 關閉。"""
        self.close_calls += 1


class CountingLease(Lease):
    """記錄 limiter lease 的 release 呼叫次數。"""

    def __init__(self, lease: Lease) -> None:
        """以已保留的 limiter slot 建立追蹤 lease。"""
        super().__init__(lease.limiter, lease.kind, lease.client_ip)
        self.release_calls = 0

    async def release(self) -> None:
        """記錄呼叫並沿用 lease 的冪等釋放。"""
        self.release_calls += 1
        await super().release()


class FailingReleaseLease(Lease):
    """在實際釋放 reservation 後模擬 lease cleanup 例外。"""

    def __init__(self, lease: Lease) -> None:
        """以已保留的 limiter slot 建立失敗追蹤 lease。"""
        super().__init__(lease.limiter, lease.kind, lease.client_ip)
        self.release_calls = 0

    async def release(self) -> None:
        """只呼叫一次原始釋放，再拋出受控 cleanup 例外。"""
        self.release_calls += 1
        await super().release()
        raise RuntimeError("lease cleanup failed")


class OneResponseMediaClient:
    """回傳單一受控 upstream response。"""

    read_timeout = 30.0

    def __init__(self, response: MediaResponse) -> None:
        """保存要交給下載 route 的 response。"""
        self.response = response

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """不接觸網路並回傳受控 response。"""
        return self.response


class FailingCloseResponse(MediaResponse):
    """Raise during upstream cleanup after a valid response body was read."""

    async def close(self) -> None:
        """Simulate a transport cleanup failure."""
        raise RuntimeError("cleanup failed")


class CountingFailingCloseResponse(MediaResponse):
    """記錄 close 次數並在 prevalidation cleanup 時拋出例外。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化可追蹤 close 次數的 upstream response。"""
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    async def close(self) -> None:
        """記錄一次 close 嘗試後拋出受控 cleanup 例外。"""
        self.close_calls += 1
        raise RuntimeError("response cleanup failed")


class CountingResponse(MediaResponse):
    """記錄 generated preview source response 的 close 次數。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化可追蹤 close ownership 的 response。"""
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        self.close_finished = asyncio.Event()

    async def close(self) -> None:
        """記錄一次 source cleanup。"""
        self.close_calls += 1
        self.close_finished.set()


class SlowCloseResponse(MediaResponse):
    """Delay response cleanup beyond a generated-preview deadline."""

    async def close(self) -> None:
        """Wait until the bounded cleanup cancels this close operation."""
        await asyncio.sleep(1)


class CancellationResistantCloseResponse(MediaResponse):
    """模擬取消後仍等待外部訊號才結束的 upstream close。"""

    def __init__(self, *args: Any, close_error: BaseException | None = None, **kwargs: Any) -> None:
        """建立可追蹤取消、完成與延遲例外的 response。"""
        super().__init__(*args, **kwargs)
        self.close_error = close_error
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        """等待釋放訊號，並在取消後仍模擬不立即退出的 close。"""
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            await self.release_close.wait()
        finally:
            self.close_finished.set()
        if self.close_error is not None:
            raise self.close_error


class FailingCloseMediaClient:
    """Return one response whose cleanup fails after streaming."""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """Return a valid image response with a failing close operation."""
        body = b"\xff\xd8\xff\xe0fake-image"
        return FailingCloseResponse(
            200,
            {"content-type": "image/jpeg", "content-length": str(len(body))},
            Reader(body),
            Writer(),
            max_bytes=100,
        )


class FailingFetchMediaClient:
    """在 response 建立前回傳受控 upstream AppError。"""

    async def fetch(self, _url: str, *, headers: Any) -> MediaResponse:
        """模擬 fetch failure 並保留可辨識的原始 AppError。"""
        raise AppError("upstream_media_invalid", "safe fetch failure")


class FakeMediaClient:
    """Return an in-memory JPEG response for every authorized token."""

    read_timeout = 30.0

    def __init__(self, *, status_code: int = 200, content_type: str = "image/jpeg") -> None:
        """Initialize captured upstream request headers for boundary assertions."""
        self.last_headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.status_code = status_code
        self.content_type = content_type

    async def fetch(self, url: str, *, headers: Any) -> MediaResponse:
        """Return a valid image response without contacting a CDN."""
        self.last_headers = dict(headers)
        self.requests.append((url, self.last_headers))
        body = b"\xff\xd8\xff\xe0fake-image"
        return MediaResponse(
            self.status_code,
            {"content-type": self.content_type, "content-length": str(len(body))},
            Reader(body),
            Writer(),
            max_bytes=100,
        )


def make_client(
    *,
    settings: Settings | None = None,
    media_client: Any = _USE_FAKE_MEDIA_CLIENT,
) -> TestClient:
    """建立使用受控 extractor 與 media transport 的 API client。"""
    settings = settings or Settings()
    selected_media_client = (
        FakeMediaClient() if media_client is _USE_FAKE_MEDIA_CLIENT else media_client
    )
    service = ExtractionService(
        settings,
        extractor=FakeExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    return TestClient(
        create_app(
            settings=settings,
            extraction_service=service,
            media_client=selected_media_client,
        )
    )


def make_download_record() -> PrivateMediaRecord:
    """建立 deterministic 的私有影片下載紀錄。"""
    return PrivateMediaRecord(
        token="download-token",
        purpose="download",
        source_url="https://video.twimg.com/1.mp4",
        media_class="video",
        filename="video.mp4",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )


def make_preview_record() -> PrivateMediaRecord:
    """建立 deterministic 的私有圖片預覽紀錄。"""
    return PrivateMediaRecord(
        token="preview-token",
        purpose="preview",
        source_url="https://pbs.twimg.com/media/1.jpg?name=small",
        media_class="image",
        filename="image.jpg",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )


def make_counting_lease() -> CountingLease:
    """保留一個下載 slot 並回傳可計次的 lease。"""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    return CountingLease(limiter.acquire_download("test-client"))


def make_tracked_media_response(
    reader: Reader,
    headers: dict[str, str],
    *,
    max_bytes: int = 100,
    read_timeout: float = 1.0,
) -> tuple[MediaResponse, CountingWriter]:
    """建立會追蹤 upstream close 的真實 MediaResponse。"""
    writer = CountingWriter()
    return (
        MediaResponse(
            200,
            headers,
            reader,
            writer,
            max_bytes=max_bytes,
            read_timeout=read_timeout,
        ),
        writer,
    )


def download_event_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, Any]]]:
    """擷取下載測試中的結構化事件與 logger message。"""
    return [
        (record.getMessage(), record.__dict__["event"])
        for record in caplog.records
        if record.name == "sns_media_list" and "event" in record.__dict__
    ]


def make_asgi_scope(*, spec_version: str) -> dict[str, Any]:
    """建立下載 response lifecycle 使用的最小 ASGI HTTP scope。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/media/download-token/download",
        "raw_path": b"/api/media/download-token/download",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.10", 1234),
        "server": ("127.0.0.1", 8000),
    }


def public_media_resolver(
    _host: str,
    _port: int,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """回傳 deterministic public IP 供真正 MediaClient policy 驗證。"""
    return [ipaddress.ip_address("93.184.216.34")]


def test_download_and_preview_use_bound_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """驗證下載與預覽 endpoint 都使用安全的 token 串流路徑。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    client = make_client()
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    media = extraction["media"][0]

    download = client.get(media["download_url"])
    preview = client.get(media["preview_url"])

    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["referrer-policy"] == "no-referrer"
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["referrer-policy"] == "no-referrer"
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "sns_media_list" and record.getMessage().startswith("media_download_")
    ] == ["media_download_started", "media_download_completed"]


@pytest.mark.asyncio
async def test_download_streams_complete_body_forwards_length_and_cleans_up_once() -> None:
    """完整轉送合法長度的 body，並只清理 upstream 與 lease 一次。"""
    reader = SequencedReader([(0, b"ab"), (0, b"cd")])
    upstream, writer = make_tracked_media_response(
        reader,
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()

    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 正常串流不應讀取 disconnect event。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集真實 ASGI response lifecycle 送出的訊息。"""
        messages.append(message)

    await response(make_asgi_scope(spec_version="2.4"), receive, send)
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )

    assert body == b"abcd"
    assert writer.close_calls == 1
    assert lease.release_calls == 1
    assert response.headers["content-length"] == "4"


@pytest.mark.asyncio
async def test_download_send_timeout_detaches_blackhole_and_releases_lease(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """下載 downstream send blackhole 時應 bounded 結束、清理並記錄安全 idle error。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    media_client = OneResponseMediaClient(upstream)
    media_client.read_timeout = 0.01
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        media_client,
        preview=False,
        lease=lease,
        response_timeout=None,
        request_id="request-send-timeout",
    )
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()
    send_finished = asyncio.Event()
    release_send = asyncio.Event()

    async def receive() -> dict[str, Any]:
        """下載 response 不應因 attachment 沒有整體 deadline 而讀取 disconnect。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    async def send(message: dict[str, Any]) -> None:
        """模擬取消後仍等待釋放的 downstream blackhole send。"""
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        send_started.set()
        try:
            await release_send.wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            await release_send.wait()
        finally:
            send_finished.set()

    response_task = asyncio.create_task(
        response(make_asgi_scope(spec_version="2.4"), receive, send)
    )
    try:
        await asyncio.wait_for(send_started.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({response_task}, timeout=0.2)
        assert response_task in done
        with pytest.raises(AppError) as exc_info:
            response_task.result()
        assert exc_info.value.message == "The downstream response timed out."
        assert send_cancelled.is_set()
        assert writer.close_calls == 1
        assert lease.release_calls == 1
        events = download_event_records(caplog)
        assert [name for name, _event in events] == [
            "media_download_started",
            "media_download_failed",
        ]
        assert events[1][1]["reason_code"] == "idle_timeout"
        assert "blackhole" not in str(events[1][1])
    finally:
        release_send.set()
        if not response_task.done():
            response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)
        await asyncio.wait_for(send_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_attachment_send_timeout_is_idle_only_and_allows_long_active_response() -> None:
    """attachment 每次 send 可受 idle bound，整體傳輸仍可超過 response deadline。"""
    upstream, _writer = make_tracked_media_response(
        SequencedReader([(0, b"a"), (0, b"b")]),
        {"content-type": "video/mp4", "content-length": "2"},
    )
    media_client = OneResponseMediaClient(upstream)
    media_client.read_timeout = 0.05
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        media_client,
        preview=False,
        lease=lease,
        response_timeout=None,
    )
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        """attachment response 不應啟用整體 response deadline 的 receive。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    async def send(message: dict[str, Any]) -> None:
        """讓每次 downstream send 保持正常但使整體傳輸超過短 deadline。"""
        messages.append(message)
        await asyncio.sleep(0.02)

    started = asyncio.get_running_loop().time()
    await response(make_asgi_scope(spec_version="2.4"), receive, send)

    assert response.send_timeout == 0.05
    assert asyncio.get_running_loop().time() - started > 0.05
    assert lease.release_calls == 1
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_download_send_caller_cancellation_rethrows_and_releases_lease() -> None:
    """caller 在 downstream send 中取消時應重拋取消且不遺留 lease。"""
    upstream, _writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    media_client = OneResponseMediaClient(upstream)
    media_client.read_timeout = 1.0
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        media_client,
        preview=False,
        lease=lease,
        response_timeout=None,
    )
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()
    send_finished = asyncio.Event()
    release_send = asyncio.Event()

    async def receive() -> dict[str, Any]:
        """caller cancellation regression 不應讀取 ASGI disconnect。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    async def send(message: dict[str, Any]) -> None:
        """模擬 caller cancellation 後仍等待釋放的 downstream send。"""
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        send_started.set()
        try:
            await release_send.wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            await release_send.wait()
        finally:
            send_finished.set()

    response_task = asyncio.create_task(
        response(make_asgi_scope(spec_version="2.4"), receive, send)
    )
    try:
        await asyncio.wait_for(send_started.wait(), timeout=0.1)
        response_task.cancel()
        done, _pending = await asyncio.wait({response_task}, timeout=0.2)
        assert response_task in done
        with pytest.raises(asyncio.CancelledError):
            response_task.result()
        assert send_cancelled.is_set()
        assert lease.release_calls == 1
        assert lease.released is True
    finally:
        release_send.set()
        if not response_task.done():
            response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)
        await asyncio.wait_for(send_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_generated_preview_uses_media_read_timeout_for_downstream_send() -> None:
    """generated preview 保留完整 deadline，且 downstream send 沿用 media idle timeout。"""
    media_client = FakeMediaClient(content_type="video/mp4")
    media_client.read_timeout = 0.07
    lease = make_counting_lease()
    response = await _generate_preview(
        PrivateMediaRecord(
            token="generated-preview-token",
            purpose="preview",
            source_url="https://video.twimg.com/1.mp4",
            media_class="video",
            filename="video.mp4",
            platform="x",
            expires_at=9999999999.0,
            request_headers={},
            preview_mode="generated",
        ),
        media_client,
        FakeThumbnailGenerator(),
        ThumbnailCoordinator(
            ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
            max_concurrency=1,
        ),
        lease=lease,
        response_timeout=1.0,
    )

    try:
        assert response.send_timeout == 0.07
        assert response.deadline is not None
    finally:
        await response._run_cleanup()


@pytest.mark.asyncio
async def test_download_logs_safe_started_and_completed_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """成功下載應記錄安全的 started 與 completed 事件。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, _writer = make_tracked_media_response(
        SequencedReader([(0, b"ab"), (0, b"cd")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )

    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=make_counting_lease(),
        request_id="request-1",
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 正常串流不應讀取 disconnect event。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集完整 downstream lifecycle 訊息。"""
        messages.append(message)

    await response(make_asgi_scope(spec_version="2.4"), receive, send)
    assert (
        b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        == b"abcd"
    )

    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_completed",
    ]
    started = events[0][1]
    completed = events[1][1]
    assert started["request_id"] == completed["request_id"] == "request-1"
    assert started["platform"] == completed["platform"] == "x"
    assert started["media_class"] == completed["media_class"] == "video"
    assert started["outcome"] == "started"
    assert started["bytes_streamed"] == 0
    assert completed["outcome"] == "success"
    assert completed["bytes_streamed"] == 4
    assert isinstance(completed["duration_ms"], float)
    assert "source_url" not in completed
    assert "token" not in completed


@pytest.mark.asyncio
async def test_response_start_disconnect_logs_aborted_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """response headers 尚未送出 body 時的 client disconnect 也應記錄中止事件。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
        request_id="request-header-disconnect",
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 response 不應讀取 disconnect event。"""
        raise AssertionError("response-start disconnect should not read receive")

    async def send(message: dict[str, Any]) -> None:
        """在 response start 模擬 client socket disconnect。"""
        if message["type"] == "http.response.start":
            raise OSError("private disconnect detail")

    with pytest.raises(ClientDisconnect):
        await response(make_asgi_scope(spec_version="2.4"), receive, send)

    assert writer.close_calls == 1
    assert lease.release_calls == 1
    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert events[1][1]["reason_code"] == "client_disconnect"
    assert events[1][1]["bytes_streamed"] == 0
    assert "private disconnect detail" not in str(events[1][1])


@pytest.mark.asyncio
async def test_body_send_disconnect_does_not_count_unsent_chunk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """first body chunk 的 downstream send 失敗時不得計入 streamed bytes。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
        request_id="request-body-disconnect",
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 response 不應讀取 disconnect event。"""
        raise AssertionError("body send disconnect should not read receive")

    async def send(message: dict[str, Any]) -> None:
        """在 first body chunk 模擬 downstream send 失敗。"""
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("private send failure")

    with pytest.raises(ClientDisconnect):
        await response(make_asgi_scope(spec_version="2.4"), receive, send)

    assert writer.close_calls == 1
    assert lease.release_calls == 1
    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert events[1][1]["reason_code"] == "client_disconnect"
    assert events[1][1]["bytes_streamed"] == 0
    assert "private send failure" not in str(events[1][1])


@pytest.mark.asyncio
async def test_final_body_send_failure_logs_aborted_not_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """最後的空 body send 失敗時不得先記錄 completed。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
        request_id="request-final-send-failure",
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 response 不應讀取 disconnect event。"""
        raise AssertionError("final body send failure should not read receive")

    async def send(message: dict[str, Any]) -> None:
        """允許媒體 body 送出，並讓最後空 body send 失敗。"""
        if message["type"] == "http.response.body" and not message.get("body"):
            raise OSError("private final send failure")

    with pytest.raises(ClientDisconnect):
        await response(make_asgi_scope(spec_version="2.4"), receive, send)

    assert writer.close_calls == 1
    assert lease.release_calls == 1
    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert events[1][1]["reason_code"] == "client_disconnect"
    assert events[1][1]["bytes_streamed"] == 4


@pytest.mark.asyncio
async def test_logging_failure_does_not_mask_stream_error_or_duplicate_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logging 拋例外時應保留原始 upstream error 並只清理一次。"""
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"a"), (0, OSError("private upstream failure"))]),
        {"content-type": "video/mp4", "content-length": "2"},
    )
    lease = make_counting_lease()

    def raise_logging_error(*_args: Any, **_kwargs: Any) -> None:
        """模擬結構化 logger 本身失敗。"""
        raise RuntimeError("private logging failure")

    monkeypatch.setattr("sns_media_list.api.routes._log_download_event", raise_logging_error)

    with pytest.raises(AppError) as exc_info:
        response = await _stream_media(
            make_download_record(),
            OneResponseMediaClient(upstream),
            preview=False,
            lease=lease,
            request_id="request-logging-failure",
        )
        _ = [chunk async for chunk in response.body_iterator]

    assert exc_info.value.code == "upstream_media_invalid"
    assert writer.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [
        ("timeout", "idle_timeout"),
        ("size", "size_limit"),
        ("truncation", "upstream_truncation"),
        ("unexpected", "unexpected_upstream_failure"),
    ],
)
@pytest.mark.asyncio
async def test_download_logs_bounded_failure_reasons(
    failure_kind: str,
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """下載失敗應只記錄 bounded reason code，不記錄原始例外內容。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    if failure_kind == "timeout":
        reader = SequencedReader([(0.05, b"x")])
        headers = {"content-type": "video/mp4", "content-length": "1"}
        max_bytes = 100
        read_timeout = 0.01
    elif failure_kind == "size":
        reader = SequencedReader([(0, b"x")])
        headers = {"content-type": "video/mp4", "content-length": "101"}
        max_bytes = 100
        read_timeout = 1.0
    elif failure_kind == "truncation":
        reader = SequencedReader([(0, b"x"), (0, b"")])
        headers = {"content-type": "video/mp4", "content-length": "2"}
        max_bytes = 100
        read_timeout = 1.0
    else:
        reader = SequencedReader([(0, b"x"), (0, OSError("private upstream detail"))])
        headers = {"content-type": "video/mp4", "content-length": "2"}
        max_bytes = 100
        read_timeout = 1.0
    upstream, _writer = make_tracked_media_response(
        reader,
        headers,
        max_bytes=max_bytes,
        read_timeout=read_timeout,
    )

    with pytest.raises(AppError):
        response = await _stream_media(
            make_download_record(),
            OneResponseMediaClient(upstream),
            preview=False,
            lease=make_counting_lease(),
            request_id="request-failure",
        )
        _ = [chunk async for chunk in response.body_iterator]

    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_failed",
    ]
    failed = events[1][1]
    assert failed["outcome"] == "failed"
    assert failed["reason_code"] == expected_reason
    assert failed["bytes_streamed"] in {0, 1}
    assert "private upstream detail" not in str(failed)
    assert "https://video.twimg.com/1.mp4" not in str(failed)
    assert "download-token" not in str(failed)


@pytest.mark.asyncio
async def test_download_without_content_length_streams_without_forging_length() -> None:
    """未知長度的下載應完整轉送，且不得自行建立 Content-Length。"""
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"ab"), (0, b"cd")]),
        {"content-type": "video/mp4"},
    )
    lease = make_counting_lease()

    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"abcd"
    assert "content-length" not in response.headers
    assert writer.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_chunked_download_streams_decoded_body_without_forging_length() -> None:
    """chunked 下載應轉送 decoded bytes，且不得自行建立 Content-Length。"""
    upstream, writer = make_tracked_media_response(
        ChunkedReader(b"2\r\nab\r\n2\r\ncd\r\n0\r\n\r\n"),
        {"content-type": "video/mp4", "transfer-encoding": "chunked"},
    )
    lease = make_counting_lease()

    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"abcd"
    assert "content-length" not in response.headers
    assert writer.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.parametrize(
    "content_length",
    ["1.0", "-1", "+1", "１"],
    ids=["fraction", "negative", "plus", "non-ascii"],
)
@pytest.mark.asyncio
async def test_download_rejects_invalid_content_length_before_response_start(
    content_length: str,
) -> None:
    """格式無效、負值或非 ASCII 的 Content-Length 應在 response 前失敗。"""
    with pytest.raises(AppError) as exc_info:
        make_tracked_media_response(
            SequencedReader([(0, b"x")]),
            {"content-type": "video/mp4", "content-length": content_length},
        )

    assert exc_info.value.code == "upstream_media_invalid"


@pytest.mark.asyncio
async def test_oversized_known_content_length_fails_before_body_read_and_cleans_once() -> None:
    """已知長度超限時不得讀取 downstream body，且資源只清理一次。"""
    reader = SequencedReader([(0, b"x")])
    upstream, writer = make_tracked_media_response(
        reader,
        {"content-type": "video/mp4", "content-length": "101"},
        max_bytes=100,
    )
    lease = make_counting_lease()

    with pytest.raises(AppError) as exc_info:
        await _stream_media(
            make_download_record(),
            OneResponseMediaClient(upstream),
            preview=False,
            lease=lease,
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert reader.read_calls == 0
    assert writer.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_unknown_length_overflow_stops_after_partial_body_and_cleans_once() -> None:
    """未知長度累計超限時應保留已送 bytes、回傳 AppError 並只清理一次。"""
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"ab"), (0, b"cd")]),
        {"content-type": "video/mp4"},
        max_bytes=3,
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )
    iterator = response.body_iterator.__aiter__()
    sent_chunks = [await anext(iterator)]

    with pytest.raises(AppError) as exc_info:
        await anext(iterator)

    assert exc_info.value.code == "upstream_media_invalid"
    assert b"".join(sent_chunks) == b"ab"
    assert writer.close_calls == 1
    assert lease.release_calls == 1


def test_slow_active_download_outlives_route_response_deadline() -> None:
    """每次讀取皆保持活動時，下載可超過舊的完整 response deadline。"""
    reader = SequencedReader([(0.01, bytes([value])) for value in range(4)])
    upstream, writer = make_tracked_media_response(
        reader,
        {"content-type": "image/jpeg", "content-length": "4"},
        read_timeout=0.1,
    )
    settings = Settings(media_response_timeout_seconds=0.025)
    client = make_client(
        settings=settings,
        media_client=OneResponseMediaClient(upstream),
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["download_url"])

    assert response.status_code == 200
    assert response.content == bytes(range(4))
    assert reader.read_calls == 4
    assert writer.close_calls == 1


def test_slow_active_download_uses_idle_timeout_without_transport_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真正 MediaClient path 只受 per-read idle timeout 約束，沒有 total deadline。"""
    body = bytes(range(12))
    reader = TransportReader(
        b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 12\r\n\r\n",
        [(0.01, bytes([value])) for value in body],
    )
    writer = CountingWriter()

    async def fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        """回傳受控 socket，同時保留真正 MediaClient transport 流程。"""
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr("sns_media_list.app.resolve_system", public_media_resolver)
    settings = Settings(
        connect_timeout_seconds=0.2,
        read_timeout_seconds=0.2,
        max_download_bytes=100,
    )
    client = make_client(settings=settings, media_client=None)
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["download_url"])

    assert response.status_code == 200
    assert response.content == body
    assert reader.read_calls == len(body)
    assert writer.writes
    assert writer.close_calls == 1


@pytest.mark.asyncio
async def test_stalled_download_read_times_out_and_cleans_up_once() -> None:
    """單次讀取超過 idle timeout 時應回傳 AppError 並只清理一次。"""
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"a"), (0.05, b"b")]),
        {"content-type": "video/mp4", "content-length": "2"},
        read_timeout=0.01,
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.body_iterator]

    assert exc_info.value.code == "upstream_media_invalid"
    assert writer.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.asyncio
async def test_upstream_body_error_closes_response_and_releases_lease_once() -> None:
    """upstream body read 失敗時應關閉 response 並只釋放 lease 一次。"""
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"a"), (0, OSError("upstream read failed"))]),
        {"content-type": "video/mp4", "content-length": "2"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
    )

    with pytest.raises(AppError) as exc_info:
        _ = [chunk async for chunk in response.body_iterator]

    assert exc_info.value.code == "upstream_media_invalid"
    assert writer.close_calls == 1
    assert lease.release_calls == 1


def test_disabled_generated_preview_releases_its_download_lease() -> None:
    """Verify defense-in-depth rejection does not consume a download slot."""
    settings = Settings(max_downloads=1, max_downloads_per_client=1)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    preview_token = service.token_store.reserve(
        [
            MediaTokenDraft(
                purpose="preview",
                source_url="https://video.twimg.com/1.mp4",
                media_class="video",
                filename="video.mp4",
                platform="x",
                request_headers={},
                preview_mode="generated",
            )
        ]
    )[0].token
    download_token = service.token_store.reserve(
        [
            MediaTokenDraft(
                purpose="download",
                source_url="https://video.twimg.com/1.mp4",
                media_class="video",
                filename="video.mp4",
                platform="x",
                request_headers={},
            )
        ]
    )[0].token
    client = TestClient(
        create_app(
            settings=settings,
            extraction_service=service,
            media_client=FakeMediaClient(content_type="video/mp4"),
        )
    )

    rejected = client.get(f"/api/media/{preview_token}/preview")
    download = client.get(f"/api/media/{download_token}/download")

    assert rejected.status_code == 404
    assert download.status_code == 200


@pytest.mark.asyncio
async def test_generated_preview_holds_lease_until_response_completion() -> None:
    """Verify generated preview capacity includes the downstream response lifetime."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1, max_downloads_per_client=1)
    lease = limiter.acquire_download("test-client")
    record = PrivateMediaRecord(
        token="preview-token",
        purpose="preview",
        source_url="https://video.twimg.com/1.mp4",
        media_class="video",
        filename="video.mp4",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
        preview_mode="generated",
    )
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
        max_concurrency=1,
    )

    response = await _generate_preview(
        record,
        FakeMediaClient(content_type="video/mp4"),
        FakeThumbnailGenerator(),
        coordinator,
        lease=lease,
        response_timeout=1.0,
    )

    assert lease.released is False

    async def receive() -> dict[str, Any]:
        """Allow the response stream to finish before reporting disconnect."""
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, Any]) -> None:
        """Accept generated response messages in the lifecycle probe."""

    await response(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/media/preview-token/preview",
            "raw_path": b"/api/media/preview-token/preview",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.10", 1234),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )

    assert lease.released is True


def test_download_head_preflight_validates_token_without_fetching_upstream() -> None:
    """Verify HEAD preflight validates a download token without opening the CDN."""
    client = make_client()
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    media = extraction["media"][0]

    response = client.head(media["download_url"])

    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"


def test_download_head_preflight_does_not_consume_media_attempt_budget() -> None:
    """Verify a native download preflight leaves the actual media attempt available."""
    settings = Settings(rate_limit_media_attempts=1)
    service = ExtractionService(
        settings,
        extractor=FakeExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(
        create_app(
            settings=settings,
            extraction_service=service,
            media_client=FakeMediaClient(),
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    download_url = extraction["media"][0]["download_url"]

    assert client.head(download_url).status_code == 204
    assert client.get(download_url).status_code == 200


def test_authenticated_story_delivery_uses_only_fixed_instagram_headers(tmp_path) -> None:
    """Verify Story preview and download never forward configured authentication."""
    cookie_file = tmp_path / "instagram.cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tstory-cookie-secret\n",
        encoding="utf-8",
    )
    settings = Settings(instagram_cookie_file=str(cookie_file))
    media_client = FakeMediaClient()
    service = ExtractionService(
        settings,
        extractor=StoryImageExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(
        create_app(settings=settings, extraction_service=service, media_client=media_client)
    )
    story_url = "https://www.instagram.com/stories/example.user/1111111111111111111/"

    extraction = client.post("/api/extractions", json={"url": story_url})
    assert extraction.status_code == 200
    media = extraction.json()["media"][0]
    preview = client.get(media["preview_url"])
    download = client.get(media["download_url"])

    assert preview.status_code == 200
    assert download.status_code == 200
    expected_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": "https://www.instagram.com/",
    }
    assert media_client.requests == [
        (
            "https://scontent.cdninstagram.com/story-image-preview.jpg?source=private",
            expected_headers,
        ),
        (
            "https://scontent.cdninstagram.com/story-image.jpg?source=private",
            expected_headers,
        ),
    ]
    for _url, headers in media_client.requests:
        assert "Cookie" not in headers
        assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_stream_cleanup_releases_download_lease_when_close_fails() -> None:
    """Verify response cleanup cannot strand a download limiter reservation."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    lease = limiter.acquire_download("test-client")
    record = PrivateMediaRecord(
        token="download-token",
        purpose="download",
        source_url="https://pbs.twimg.com/media/1.jpg?name=orig",
        media_class="image",
        filename="image.jpg",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )

    response = await _stream_media(
        record,
        FailingCloseMediaClient(),
        preview=False,
        lease=lease,
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        _ = [chunk async for chunk in response.body_iterator]

    assert lease.released is True


@pytest.mark.asyncio
async def test_client_disconnect_closes_upstream_and_releases_lease_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """downstream 傳送中斷時應關閉 upstream 並只釋放 lease 一次。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    reader = BlockingSecondReadReader()
    upstream, writer = make_tracked_media_response(
        reader,
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
        request_id="request-disconnect",
    )

    async def receive() -> dict[str, Any]:
        """等第二次 upstream read 阻塞後回報 client disconnect。"""
        await reader.blocked.wait()
        return {"type": "http.disconnect"}

    sent_chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        """記錄 disconnect 前真正送出的 body bytes。"""
        if message["type"] == "http.response.body" and message.get("body"):
            sent_chunks.append(message["body"])

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(
            response(make_asgi_scope(spec_version="2.3"), receive, send),
            timeout=0.5,
        )

    assert b"".join(sent_chunks) == b"ab"
    assert reader.read_calls == 2
    assert reader.blocked.is_set()
    assert reader.cancelled.is_set()
    assert writer.close_calls == 1
    assert lease.release_calls == 1
    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert events[1][1]["outcome"] == "aborted"
    assert events[1][1]["reason_code"] == "client_disconnect"
    assert events[1][1]["bytes_streamed"] == 2


@pytest.mark.asyncio
async def test_asgi_23_body_disconnect_logs_aborted_without_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ASGI 2.3 在 body send 中收到 disconnect 時應記錄 aborted 並只清理一次。"""
    caplog.set_level(logging.INFO, logger="sns_media_list")
    upstream, writer = make_tracked_media_response(
        SequencedReader([(0, b"body")]),
        {"content-type": "video/mp4", "content-length": "4"},
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(upstream),
        preview=False,
        lease=lease,
        request_id="request-asgi-23-body-disconnect",
    )
    body_send_started = asyncio.Event()
    body_send_cancelled = asyncio.Event()
    sent_types: list[str] = []

    async def receive() -> dict[str, Any]:
        """在 response body send 開始後回傳真實 ASGI disconnect event。"""
        await body_send_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        """記錄 response-start/body 並讓 body send 等待 disconnect 取消。"""
        sent_types.append(message["type"])
        if message["type"] == "http.response.body" and message.get("body"):
            body_send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                body_send_cancelled.set()
                raise

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(
            response(make_asgi_scope(spec_version="2.3"), receive, send),
            timeout=0.5,
        )

    assert sent_types == ["http.response.start", "http.response.body"]
    assert body_send_cancelled.is_set()
    assert writer.close_calls == 1
    assert lease.release_calls == 1
    events = download_event_records(caplog)
    assert [name for name, _event in events] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert events[1][1]["outcome"] == "aborted"
    assert events[1][1]["reason_code"] == "client_disconnect"
    assert events[1][1]["bytes_streamed"] == 0


@pytest.mark.asyncio
async def test_stream_fetch_deadline_releases_download_lease() -> None:
    """Verify a complete media deadline includes upstream fetch and releases its lease."""
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    lease = limiter.acquire_download("test-client")
    record = PrivateMediaRecord(
        token="download-token",
        purpose="download",
        source_url="https://pbs.twimg.com/media/1.jpg?name=orig",
        media_class="image",
        filename="image.jpg",
        platform="x",
        expires_at=9999999999.0,
        request_headers={},
    )

    with pytest.raises(AppError, match="deadline"):
        await _stream_media(
            record,
            SlowMediaClient(),
            preview=False,
            lease=lease,
            response_timeout=0.01,
        )

    assert lease.released is True


@pytest.mark.asyncio
async def test_stream_validation_cleanup_preserves_primary_error_when_cleanup_fails() -> None:
    """prevalidation 的 close/release 例外不得覆蓋原始 AppError。"""
    response = CountingFailingCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "4"},
        Reader(b"body"),
        Writer(),
        max_bytes=100,
    )
    lease = FailingReleaseLease(make_counting_lease())

    with pytest.raises(AppError) as exc_info:
        await _stream_media(
            make_download_record(),
            OneResponseMediaClient(response),
            preview=False,
            lease=lease,
        )

    assert exc_info.value.code == "upstream_media_invalid"
    assert response.close_calls == 1
    assert lease.release_calls == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_stream_fetch_cleanup_preserves_primary_error_when_release_fails() -> None:
    """fetch failure 時的 lease cleanup 例外不得覆蓋原始 upstream AppError。"""
    lease = FailingReleaseLease(make_counting_lease())

    with pytest.raises(AppError, match="safe fetch failure"):
        await _stream_media(
            make_download_record(),
            FailingFetchMediaClient(),
            preview=False,
            lease=lease,
        )

    assert lease.release_calls == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_generated_preview_preserves_primary_error_when_release_fails() -> None:
    """預覽生成原始 AppError 時，lease release 例外不得覆蓋原始錯誤。"""
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
        max_concurrency=1,
    )
    lease = FailingReleaseLease(make_counting_lease())

    with pytest.raises(AppError, match="safe fetch failure"):
        await _generate_preview(
            make_preview_record(),
            FailingFetchMediaClient(),
            FakeThumbnailGenerator(),
            coordinator,
            lease=lease,
            response_timeout=1.0,
        )

    assert lease.release_calls == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_generated_preview_preserves_primary_error_when_response_close_fails() -> None:
    """預覽生成原始錯誤時，response close 例外不得覆蓋原始錯誤。"""
    body = b"\xff\xd8\xff\xe0fake-image"
    response = FailingCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(body))},
        Reader(body),
        Writer(),
        max_bytes=100,
    )
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
        max_concurrency=1,
    )

    with pytest.raises(AppError, match="primary thumbnail failure"):
        await _generate_preview(
            make_preview_record(),
            OneResponseMediaClient(response),
            PrimaryFailingThumbnailGenerator(),
            coordinator,
            lease=make_counting_lease(),
            response_timeout=1.0,
        )


@pytest.mark.asyncio
async def test_generated_preview_closes_source_exactly_once_after_success() -> None:
    """generated preview 成功時 route owner 應只 close source 一次。"""
    source = CountingResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "0"},
        Reader(b""),
        Writer(),
        max_bytes=100,
    )
    generated = await _generate_preview(
        make_preview_record(),
        OneResponseMediaClient(source),
        FakeThumbnailGenerator(),
        ThumbnailCoordinator(
            ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
            max_concurrency=1,
        ),
        lease=make_counting_lease(),
        response_timeout=1.0,
    )

    assert source.close_calls == 1
    await generated._run_cleanup()


@pytest.mark.asyncio
async def test_generated_preview_closes_source_exactly_once_after_failure() -> None:
    """generated preview 失敗時 route owner 仍應只 close source 一次。"""
    source = CountingResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "0"},
        Reader(b""),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError, match="primary thumbnail failure"):
        await _generate_preview(
            make_preview_record(),
            OneResponseMediaClient(source),
            PrimaryFailingThumbnailGenerator(),
            ThumbnailCoordinator(
                ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
                max_concurrency=1,
            ),
            lease=make_counting_lease(),
            response_timeout=1.0,
        )

    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_generated_preview_closes_source_exactly_once_after_cancellation() -> None:
    """generated preview 被取消時 route owner 仍應只 close source 一次。"""
    source = CountingResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "0"},
        Reader(b""),
        Writer(),
        max_bytes=100,
    )
    generator = BlockingThumbnailGenerator()
    task = asyncio.create_task(
        _generate_preview(
            make_preview_record(),
            OneResponseMediaClient(source),
            generator,
            ThumbnailCoordinator(
                ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
                max_concurrency=1,
            ),
            lease=make_counting_lease(),
            response_timeout=1.0,
        )
    )

    try:
        await generator.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(source.close_finished.wait(), timeout=0.1)
        assert source.close_calls == 1
    finally:
        generator.release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_generated_preview_process_stop_error_preserves_primary_and_closes_source_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg terminate OS error 不得覆蓋 primary，且 source 仍只 close 一次。"""
    process = ThumbnailProcess(b"not-a-jpeg", fail_terminate=True)

    async def fake_create(*_args: Any, **_kwargs: Any) -> ThumbnailProcess:
        """回傳 terminate 會失敗且 output 無效的受控 process。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    source_body = b"\xff\xd8\xff\xe0source-image\xff\xd9"
    source = CountingResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(source_body))},
        Reader(source_body),
        Writer(),
        max_bytes=100,
    )

    with pytest.raises(AppError, match="generated thumbnail is invalid"):
        await _generate_preview(
            make_preview_record(),
            OneResponseMediaClient(source),
            ThumbnailGenerator(input_bytes=100, output_bytes=100),
            ThumbnailCoordinator(
                ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
                max_concurrency=1,
            ),
            lease=make_counting_lease(),
            response_timeout=1.0,
        )

    await asyncio.wait_for(source.close_finished.wait(), timeout=0.1)
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_generated_response_cleanup_is_deadline_bounded() -> None:
    """Verify generated-preview cleanup cannot occupy a slot beyond its deadline."""
    body = b"\xff\xd8\xff\xe0fake-image"
    response = SlowCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(body))},
        Reader(body),
        Writer(),
        max_bytes=100,
    )

    await asyncio.wait_for(_close_response_with_timeout(response, timeout=0.01), timeout=0.1)


@pytest.mark.asyncio
async def test_generated_preview_timeout_detaches_cancellation_resistant_fetch() -> None:
    """generator timeout 應立即返回安全錯誤而不等待抗取消 fetch。"""
    media_client = CancellationResistantFetchMediaClient()
    coordinator = ThumbnailCoordinator(
        ThumbnailCache(max_bytes=1_000_000, max_negative_entries=10),
        max_concurrency=1,
    )
    task = asyncio.create_task(
        _generate_preview(
            make_preview_record(),
            media_client,
            TimedThumbnailGenerator(),
            coordinator,
            lease=make_counting_lease(),
            response_timeout=1.0,
        )
    )

    try:
        await media_client.started.wait()
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError) as exc_info:
            task.result()
        assert exc_info.value.code == "upstream_media_invalid"
        assert exc_info.value.message == "Thumbnail generation timed out."
        await asyncio.wait_for(media_client.cancelled.wait(), timeout=0.1)
    finally:
        media_client.release.set()
        await asyncio.wait_for(media_client.finished.wait(), timeout=0.2)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_stream_cleanup_without_deadline_is_bounded_and_releases_lease() -> None:
    """無完整 deadline 的下載 close 仍須有固定上限並釋放 lease。"""
    response = CancellationResistantCloseResponse(
        200,
        {"content-type": "video/mp4", "content-length": "4"},
        Reader(b"body"),
        Writer(),
        max_bytes=100,
    )
    lease = make_counting_lease()
    streaming_response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(response),
        preview=False,
        lease=lease,
    )

    async def consume() -> None:
        """消耗下載 body 以觸發 route cleanup。"""
        _ = [chunk async for chunk in streaming_response.body_iterator]

    stream_task = asyncio.create_task(consume())
    try:
        await response.close_started.wait()
        done, _pending = await asyncio.wait({stream_task}, timeout=1.1)
        assert stream_task in done
        assert lease.released is True
    finally:
        response.release_close.set()
        if not stream_task.done():
            try:
                await asyncio.wait_for(stream_task, timeout=0.2)
            except AppError:
                pass
        await asyncio.wait_for(response.close_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_cancelled_preview_cleanup_does_not_wait_for_resistant_close() -> None:
    """取消後的 preview close 不得等待 cancellation-resistant task 結束。"""
    response = CancellationResistantCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "3"},
        Reader(b"\xff\xd8\xff"),
        Writer(),
        max_bytes=100,
    )
    cleanup_task = asyncio.create_task(_close_response_with_timeout(response, timeout=0.01))
    try:
        await response.close_started.wait()
        await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=0.1)
        assert cleanup_task.done() is True
    finally:
        response.release_close.set()
        await asyncio.wait_for(cleanup_task, timeout=0.2)
        await asyncio.wait_for(response.close_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_stream_primary_error_survives_resistant_cleanup_and_releases_lease() -> None:
    """upstream body error 不得被延遲 cleanup 例外覆蓋，且 lease 必須釋放。"""
    response = CancellationResistantCloseResponse(
        200,
        {"content-type": "video/mp4", "content-length": "2"},
        SequencedReader([(0, b"a"), (0, OSError("primary upstream failure"))]),
        Writer(),
        max_bytes=100,
        close_error=RuntimeError("cleanup failure"),
    )
    lease = make_counting_lease()
    streaming_response = await _stream_media(
        make_download_record(),
        OneResponseMediaClient(response),
        preview=False,
        lease=lease,
    )

    async def consume() -> None:
        """消耗 body 以取得原始 upstream error。"""
        _ = [chunk async for chunk in streaming_response.body_iterator]

    stream_task = asyncio.create_task(consume())
    try:
        await response.close_started.wait()
        done, _pending = await asyncio.wait({stream_task}, timeout=1.1)
        assert stream_task in done
        with pytest.raises(AppError, match="could not be read"):
            stream_task.result()
        assert lease.released is True
    finally:
        response.release_close.set()
        if not stream_task.done():
            try:
                await asyncio.wait_for(stream_task, timeout=0.2)
            except AppError:
                pass
        await asyncio.wait_for(response.close_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_stream_preview_cleanup_is_bounded_after_deadline_and_releases_lease() -> None:
    """預覽 deadline 到期後，延遲 close 不得阻塞且仍須釋放 lease。"""
    body = b"\xff\xd8\xff" + b"x" * 509
    upstream = SlowCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(body))},
        Reader(body),
        Writer(),
        max_bytes=1000,
        read_timeout=300.0,
    )
    lease = make_counting_lease()
    response = await _stream_media(
        make_preview_record(),
        OneResponseMediaClient(upstream),
        preview=True,
        lease=lease,
        response_timeout=0.01,
    )

    async def receive() -> dict[str, Any]:
        """ASGI 2.4 預覽串流不應讀取 disconnect event。"""
        raise AssertionError("ASGI 2.4 response should not call receive")

    async def send(message: dict[str, Any]) -> None:
        """讓 downstream body send 持續到 response deadline 之後。"""
        if message["type"] == "http.response.body" and message.get("body"):
            await asyncio.sleep(1.0)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(
        response(make_asgi_scope(spec_version="2.4"), receive, send),
        timeout=0.2,
    )

    assert asyncio.get_running_loop().time() - started < 0.1
    assert lease.release_calls == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_preview_prevalidation_cleanup_is_bounded_and_releases_lease() -> None:
    """預覽預檢 deadline 到期時，延遲 close 不得阻塞且仍須釋放 lease。"""
    upstream = SlowCloseResponse(
        200,
        {"content-type": "image/jpeg", "content-length": "1"},
        SequencedReader([(1.0, b"x")]),
        Writer(),
        max_bytes=1000,
        read_timeout=300.0,
    )
    lease = make_counting_lease()
    started = asyncio.get_running_loop().time()

    with pytest.raises(AppError, match="deadline"):
        await _stream_media(
            make_preview_record(),
            OneResponseMediaClient(upstream),
            preview=True,
            lease=lease,
            response_timeout=0.01,
        )

    assert asyncio.get_running_loop().time() - started < 0.1
    assert lease.release_calls == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_deadline_response_preserves_upstream_timeout_error() -> None:
    """deadline 尚未到期時，body 原始 TimeoutError 不得被 response 吞掉。"""
    cleanup_calls = 0

    async def body() -> Any:
        """先送出一段資料，再模擬 upstream 自己拋出 timeout。"""
        yield b"first"
        raise TimeoutError("upstream timeout")

    async def cleanup() -> None:
        """記錄 response failure path 的 cleanup 呼叫。"""
        nonlocal cleanup_calls
        cleanup_calls += 1

    async def receive() -> dict[str, Any]:
        """ASGI 2.3 disconnect listener 在 body failure 前保持等待。"""
        await asyncio.Event().wait()
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        """接受第一段 body，讓第二次迭代觸發 upstream timeout。"""

    response = DeadlineStreamingResponse(
        body(),
        deadline=asyncio.get_running_loop().time() + 1.0,
        cleanup=cleanup,
    )

    with pytest.raises(TimeoutError, match="upstream timeout"):
        await response(make_asgi_scope(spec_version="2.3"), receive, send)

    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_await_with_deadline_preserves_raw_timeout_before_deadline() -> None:
    """自身 deadline 尚未到期時，awaitable 的 raw TimeoutError 必須保留。"""

    async def operation() -> None:
        """模擬 upstream 主動拋出 timeout。"""
        raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError, match="upstream timeout"):
        await _await_with_deadline(operation(), asyncio.get_running_loop().time() + 1.0)


@pytest.mark.asyncio
async def test_await_with_deadline_detaches_cancellation_resistant_operation() -> None:
    """deadline 到期時不得等待吞掉取消的 operation，且仍須消費其背景例外。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        """模擬取消後仍等待外部訊號才結束的 upstream operation。"""
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            finished.set()
        raise RuntimeError("detached deadline failure")

    task = asyncio.create_task(
        _await_with_deadline(operation(), asyncio.get_running_loop().time() + 0.01)
    )
    try:
        await started.wait()
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(AppError, match="deadline"):
            task.result()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.1)
    finally:
        release.set()
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass
        else:
            try:
                task.result()
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_await_with_deadline_detaches_operation_when_caller_is_cancelled() -> None:
    """caller cancellation 時不得等待仍在吞取消的 operation。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        """模擬 caller cancellation 後仍等待釋放訊號的 operation。"""
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            finished.set()

    task = asyncio.create_task(
        _await_with_deadline(operation(), asyncio.get_running_loop().time() + 1.0)
    )
    try:
        await started.wait()
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            task.result()
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    finally:
        release.set()
        if not finished.is_set():
            await asyncio.wait_for(finished.wait(), timeout=0.2)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass


@pytest.mark.parametrize("cancel_caller", [False, True], ids=["deadline", "caller-cancel"])
@pytest.mark.asyncio
async def test_stream_fetch_closes_successful_detached_response(
    cancel_caller: bool,
) -> None:
    """fetch 被 deadline 或 caller cancellation 脫離後仍回應成功時必須 bounded close。"""
    body = b"\xff\xd8\xffdetached"
    response = CountingResponse(
        200,
        {"content-type": "image/jpeg", "content-length": str(len(body))},
        Reader(body),
        Writer(),
        max_bytes=100,
    )
    media_client = SuccessfulCancellationResistantFetchMediaClient(response)
    task = asyncio.create_task(
        _stream_media(
            make_download_record(),
            media_client,
            preview=False,
            lease=make_counting_lease(),
            response_timeout=1.0 if cancel_caller else 0.01,
        )
    )

    try:
        await asyncio.wait_for(media_client.started.wait(), timeout=0.1)
        if cancel_caller:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            assert task in done
            with pytest.raises(AppError, match="deadline"):
                task.result()
        await asyncio.wait_for(media_client.cancelled.wait(), timeout=0.1)
        media_client.release.set()
        await asyncio.wait_for(response.close_finished.wait(), timeout=0.2)
        assert response.close_calls == 1
    finally:
        media_client.release.set()
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=0.2)
            except BaseException:
                pass
        else:
            try:
                task.result()
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_await_with_expired_deadline_closes_coroutine() -> None:
    """expired deadline 應關閉尚未啟動的 coroutine，避免 unawaited warning。"""

    async def operation() -> None:
        """提供不應在 expired deadline 後執行的 coroutine。"""
        raise AssertionError("expired coroutine must not run")

    coroutine = operation()
    with pytest.raises(AppError, match="deadline"):
        await _await_with_deadline(coroutine, asyncio.get_running_loop().time() - 1.0)

    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_await_with_expired_deadline_detaches_task_and_consumes_exception() -> None:
    """expired deadline 應取消 detached Task 並消費其最終例外。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        """模擬取消後仍等待釋放訊號的背景 Task。"""
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        finished.set()
        raise RuntimeError("detached deadline failure")

    task = asyncio.create_task(operation())
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 deadline cleanup observer 消費的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        await started.wait()
        with pytest.raises(AppError, match="deadline"):
            await _await_with_deadline(task, asyncio.get_running_loop().time() - 1.0)
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        release.set()
        if not finished.is_set():
            await asyncio.wait_for(finished.wait(), timeout=0.1)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_deadline_response_does_not_swallow_body_timeout_after_cancel() -> None:
    """deadline cancellation 後 body 自行拋出的 raw timeout 不得變成成功 response。"""

    async def body() -> Any:
        """將自身取消轉換成 upstream raw TimeoutError。"""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise TimeoutError("body timeout") from None
        yield b"unreachable"

    async def receive() -> dict[str, Any]:
        """保持 ASGI 2.3 disconnect listener 等待。"""
        await asyncio.Event().wait()
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        """接受測試 response 訊息。"""

    response = DeadlineStreamingResponse(
        body(),
        deadline=asyncio.get_running_loop().time() + 0.01,
    )

    with pytest.raises(TimeoutError, match="body timeout"):
        await response(make_asgi_scope(spec_version="2.3"), receive, send)


@pytest.mark.asyncio
async def test_deadline_response_reports_failure_when_body_outlives_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抗取消 body 超過 deadline 時應觸發 failure 且不得觸發 completion。"""
    monkeypatch.setattr("sns_media_list.api.routes._STREAM_CLEANUP_TIMEOUT_SECONDS", 0.01)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()
    errors: list[BaseException] = []
    complete_calls = 0

    async def body() -> Any:
        """模擬取消後仍等待外部釋放的 body generator。"""
        started.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            yield b"late body"
        finally:
            finished.set()

    async def receive() -> dict[str, Any]:
        """提供不會主動 disconnect 的 ASGI receive。"""
        await asyncio.Event().wait()
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        """接受 deadline regression response 的 ASGI 訊息。"""

    def on_error(error: BaseException) -> None:
        """收集 response deadline failure。"""
        errors.append(error)

    def on_complete() -> None:
        """記錄不應發生的 response completion。"""
        nonlocal complete_calls
        complete_calls += 1

    response = DeadlineStreamingResponse(
        body(),
        deadline=asyncio.get_running_loop().time() + 0.01,
        on_stream_error=on_error,
        on_stream_complete=on_complete,
    )

    try:
        await response(make_asgi_scope(spec_version="2.4"), receive, send)
        await asyncio.wait_for(started.wait(), timeout=0.1)
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        assert len(errors) == 1
        error = errors[0]
        assert type(error) is AppError
        assert "deadline" in str(error)
        assert complete_calls == 0
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_deadline_response_preserves_caller_cancellation_during_stream_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deadline cleanup wait 中收到 caller cancellation 時須完成 cleanup 後重拋取消。"""
    monkeypatch.setattr("sns_media_list.api.routes._STREAM_CLEANUP_TIMEOUT_SECONDS", 0.5)
    started = asyncio.Event()
    deadline_cancelled = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()
    cleanup_calls = 0

    async def body() -> Any:
        """模擬 deadline cancel 後仍等待外部釋放的 stream body。"""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            deadline_cancelled.set()
            await release.wait()
        finally:
            finished.set()
        if False:
            yield b"unreachable"

    async def cleanup() -> None:
        """記錄 caller cancellation 後仍必須完成的 response cleanup。"""
        nonlocal cleanup_calls
        cleanup_calls += 1

    async def receive() -> dict[str, Any]:
        """提供不會主動 disconnect 的 ASGI receive。"""
        await asyncio.Event().wait()
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        """接受測試 response 訊息。"""

    response = DeadlineStreamingResponse(
        body(),
        deadline=asyncio.get_running_loop().time() + 0.01,
        cleanup=cleanup,
    )
    response_task = asyncio.create_task(
        response(make_asgi_scope(spec_version="2.4"), receive, send)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=0.1)
        await asyncio.wait_for(deadline_cancelled.wait(), timeout=0.1)
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task
        assert cleanup_calls == 1
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.2)
        if not response_task.done():
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)


def test_generated_preview_is_created_lazily_and_cached() -> None:
    """Verify generated previews use the endpoint, safe headers, and one generation."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(content_type="video/mp4")
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )

    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    preview_url = extraction["media"][0]["preview_url"]

    first = client.get(preview_url)
    second = client.get(preview_url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["content-disposition"].startswith("inline;")
    assert first.headers["x-content-type-options"] == "nosniff"
    assert generator.calls == 1


def test_generated_preview_deterministic_failure_is_cached() -> None:
    """Verify repeated deterministic generation failures do not repeat FFmpeg work."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    generator = FailingThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=FakeMediaClient(content_type="video/mp4"),
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    first = client.get(extraction["media"][0]["preview_url"])
    second = client.get(extraction["media"][0]["preview_url"])

    assert first.status_code == 502
    assert second.status_code == 502
    assert generator.calls == 1


@pytest.mark.asyncio
async def test_generated_preview_saturation_returns_immediate_rate_limit() -> None:
    """Verify a different generated token is rejected while the only slot is active."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    generator = BlockingThumbnailGenerator()
    app = create_app(
        extraction_service=service,
        media_client=FakeMediaClient(content_type="video/mp4"),
        thumbnail_generator=generator,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first_extraction = (
            await client.post("/api/extractions", json={"url": "https://x.com/creator/status/1"})
        ).json()
        second_extraction = (
            await client.post("/api/extractions", json={"url": "https://x.com/creator/status/2"})
        ).json()
        first_request = asyncio.create_task(client.get(first_extraction["media"][0]["preview_url"]))
        await generator.started.wait()

        saturated = await client.get(second_extraction["media"][0]["preview_url"])
        generator.release.set()
        first_response = await first_request

    assert saturated.status_code == 429
    assert saturated.headers["Retry-After"] == "1"
    assert saturated.json()["code"] == "local_rate_limited"
    assert first_response.status_code == 200
    assert generator.calls == 1


def test_generated_preview_rejects_upstream_status_before_generation() -> None:
    """Verify an error response cannot become a thumbnail even if its body looks valid."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(status_code=404)
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502
    assert generator.calls == 0


def test_generated_preview_rejects_mismatched_expected_media_class() -> None:
    """Verify a video token cannot use an image upstream response as its source."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    media_client = FakeMediaClient(content_type="image/jpeg")
    generator = FakeThumbnailGenerator()
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=media_client,
            thumbnail_generator=generator,
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502
    assert generator.calls == 0


def test_generated_preview_deadline_covers_upstream_fetch() -> None:
    """Verify the generation deadline includes upstream connection and headers."""
    settings = Settings(generated_previews_enabled=True)
    service = ExtractionService(
        settings,
        extractor=GeneratedExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(
        create_app(
            extraction_service=service,
            media_client=SlowMediaClient(),
            thumbnail_generator=TimedThumbnailGenerator(),
        )
    )
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["preview_url"])

    assert response.status_code == 502


def test_media_proxy_sends_only_fixed_headers_without_platform_cookies() -> None:
    """Verify downstream media requests never inherit platform authentication."""
    settings = Settings()
    media_client = FakeMediaClient()
    service = ExtractionService(
        settings,
        extractor=FakeExtractor(),
        token_store=TokenStore(capacity=20, ttl_seconds=600),
    )
    client = TestClient(create_app(extraction_service=service, media_client=media_client))
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()

    response = client.get(extraction["media"][0]["download_url"])

    assert response.status_code == 200
    assert media_client.last_headers == {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": "https://x.com/",
    }


def test_preview_token_cannot_be_used_for_download() -> None:
    """Verify token purpose mismatch never contacts the media client."""
    client = make_client()
    extraction = client.post(
        "/api/extractions", json={"url": "https://x.com/creator/status/1"}
    ).json()
    preview_url = extraction["media"][0]["preview_url"]
    token = preview_url.split("/")[-2]

    response = client.get(f"/api/media/{token}/download")

    assert response.status_code == 404
    assert response.json()["code"] == "token_not_found"


def test_unknown_token_returns_not_found() -> None:
    """Verify random tokens have a stable missing-token response."""
    client = make_client()

    response = client.get("/api/media/random-token/download")

    assert response.status_code == 404
    assert response.json()["code"] == "token_not_found"


def test_generated_token_is_rejected_when_decoder_is_disabled() -> None:
    """Verify generated records cannot bypass a disabled decoder configuration."""
    settings = Settings()
    store = TokenStore(capacity=20, ttl_seconds=600)
    record = store.reserve(
        [
            MediaTokenDraft(
                purpose="preview",
                source_url="https://video.twimg.com/1.mp4",
                media_class="video",
                filename="video.mp4",
                platform="x",
                request_headers={},
                preview_mode="generated",
            )
        ]
    )[0]
    service = ExtractionService(settings, extractor=FakeExtractor(), token_store=store)
    media_client = FakeMediaClient(content_type="video/mp4")
    client = TestClient(create_app(extraction_service=service, media_client=media_client))

    response = client.get(f"/api/media/{record.token}/preview")

    assert response.status_code == 404
    assert response.json()["code"] == "token_not_found"
    assert media_client.requests == []
