"""驗證原始媒體檢視路徑的授權與串流。"""

import asyncio
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message

from sns_media_list.api.routes import StreamAborted, _stream_view
from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.network.media_viewing import ViewRange
from sns_media_list.security.tokens import MediaTokenDraft, TokenStore
from sns_media_list.services.extraction_service import ExtractionService
from tests.api.test_downloads import (
    CountingLease,
    FakeExtractor,
    make_asgi_scope,
    make_counting_lease,
    make_download_record,
)
from tests.unit.test_media_client import FakeResponseWriter


class ViewClient:
    """提供真正 MediaResponse 並記錄原始媒體存取次數。"""

    read_timeout = 1.0

    def __init__(self) -> None:
        """建立固定圖片、影片及連線清理觀測值。"""
        self.calls: list[str | None] = []
        self.writers: list[FakeResponseWriter] = []

    async def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        view_range: ViewRange | None = None,
    ) -> MediaResponse:
        """以固定 JPEG 或 MP4 bytes 模擬受限 transport 已取得的來源。"""
        self.calls.append(view_range.header if view_range else None)
        body = b"\xff\xd8\xff\xe0original-image"
        mime = "image/jpeg"
        if url.endswith(".mp4"):
            body = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"x" * 1024
            mime = "video/mp4"
        response_headers = {"content-type": mime}
        status = 200
        if view_range:
            start = view_range.start
            if start is None:
                assert view_range.end is not None
                start = max(0, len(body) - view_range.end)
                end = len(body) - 1
            else:
                end = (
                    min(view_range.end, len(body) - 1)
                    if view_range.end is not None
                    else len(body) - 1
                )
            if start >= len(body):
                response_headers["content-range"] = f"bytes */{len(body)}"
                body = b""
                status = 416
            else:
                response_headers["content-range"] = f"bytes {start}-{end}/{len(body)}"
                body = body[start : end + 1]
                status = 206
        response_headers["content-length"] = str(len(body))
        reader = asyncio.StreamReader()
        reader.feed_data(body)
        reader.feed_eof()
        writer = FakeResponseWriter()
        self.writers.append(writer)
        return MediaResponse(status, response_headers, reader, writer, max_bytes=5000)


def setup_view(media: ViewClient | None = None) -> tuple[TestClient, TokenStore, ViewClient]:
    """建立獨立 token store 與不存取外部網路的完整應用。"""
    settings = Settings(max_download_bytes=5000, rate_limit_media_attempts=100)
    store = TokenStore(capacity=20, ttl_seconds=600)
    service = ExtractionService(settings, extractor=FakeExtractor(), token_store=store)
    media = media or ViewClient()
    return (
        TestClient(create_app(settings=settings, extraction_service=service, media_client=media)),
        store,
        media,
    )


class ScriptedViewClient(ViewClient):
    """提供明確腳本回應以驗證不同 probe／fragment 證據。"""

    def __init__(self, scripts: list[tuple[int, dict[str, str], bytes]]) -> None:
        """儲存受控來源回應，不重用被測 parser 計算預期。"""
        super().__init__()
        self.scripts = scripts
        self.readers: list[asyncio.StreamReader] = []

    async def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        view_range: ViewRange | None = None,
    ) -> MediaResponse:
        """回傳真實有界 response，並確認 probe 先關閉才讀取片段。"""
        assert all(writer.closed for writer in self.writers)
        self.calls.append(view_range.header if view_range else None)
        status, response_headers, body = self.scripts.pop(0)
        reader = asyncio.StreamReader()
        reader.feed_data(body)
        reader.feed_eof()
        self.readers.append(reader)
        writer = FakeResponseWriter()
        self.writers.append(writer)
        return MediaResponse(status, response_headers, reader, writer, max_bytes=5000)


def issue(store: TokenStore, *, video: bool = False, preview: bool = False) -> str:
    """核發標準授權，避免測試直接修改私有 token record。"""
    return store.reserve(
        [
            MediaTokenDraft(
                purpose="preview" if preview else "download",
                media_class="video" if video else "image",
                filename="file.mp4" if video else "file.jpg",
                platform="x",
                request_headers={},
                source_url="https://video.twimg.com/file.mp4"
                if video
                else "https://pbs.twimg.com/file.jpg",
            )
        ]
    )[0].token


def test_view_head_is_token_only() -> None:
    """HEAD 即使帶非法 Range 也只驗授權，不碰來源。"""
    client, store, media = setup_view()
    token = issue(store)
    original = store.get(token, "download")
    with client:
        result = client.head(f"/api/media/{token}/view", headers={"Range": "invalid"})
    assert result.status_code == 204
    assert not result.content
    assert media.calls == []
    assert store.get(token, "download").expires_at == original.expires_at
    assert store.size == 1


@pytest.mark.parametrize("method", ["get", "head"])
def test_view_expired_token_never_fetches(method: str) -> None:
    """尚在 store 的過期 token 回 410，重啟後未知 token 回 404。"""
    client, store, media = setup_view()
    token = issue(store)
    store.clock = lambda: 999999999999.0
    with client:
        result = client.request(method, f"/api/media/{token}/view")
    assert result.status_code == 410
    assert result.headers["x-sns-error-code"] == "token_expired"
    assert media.calls == []


def test_view_unsatisfiable_range_has_safe_total() -> None:
    """416 僅由已驗證 total 建立 Content-Range，不轉送來源 body。"""
    client, store, media = setup_view()
    token = issue(store, video=True)
    with client:
        result = client.get(f"/api/media/{token}/view", headers={"Range": "bytes=2000-2005"})
    assert result.status_code == 416
    assert result.headers["content-range"] == "bytes */1048"
    assert result.json()["code"] == "view_range_not_satisfiable"
    assert all(writer.closed for writer in media.writers)


@pytest.mark.parametrize("method", ["get", "head"])
def test_view_rejects_wrong_purpose_or_missing_token(method: str) -> None:
    """檢視不可將 preview token 升權，未知 token 不觸發來源請求。"""
    client, store, media = setup_view()
    preview = issue(store, preview=True)
    with client:
        for token in (preview, "unknown"):
            result = client.request(method, f"/api/media/{token}/view", headers={"Range": "bad"})
            assert result.status_code == 404
    assert media.calls == []


def test_view_streams_original_image_inline() -> None:
    """有效下載授權能取得原圖而非縮圖，且回應為被動 inline。"""
    client, store, media = setup_view()
    token = issue(store)
    with client:
        result = client.get(f"/api/media/{token}/view")
    assert result.status_code == 200
    assert result.content == b"\xff\xd8\xff\xe0original-image"
    assert result.headers["content-disposition"].startswith("inline;")
    assert result.headers["cache-control"] == "no-store"
    assert media.calls == [None]
    assert all(writer.closed for writer in media.writers)


def test_view_streams_complete_video_without_resume() -> None:
    """完整影片使用原始 200 bytes，不偷偷套用 attachment resume。"""
    client, store, media = setup_view()
    token = issue(store, video=True)
    with client:
        result = client.get(f"/api/media/{token}/view")
    assert result.status_code == 200
    assert result.content == b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"x" * 1024
    assert "content-range" not in result.headers
    assert media.calls == [None]


@pytest.mark.parametrize("preview", [False, True])
def test_view_range_does_not_change_original_endpoints(preview: bool) -> None:
    """既有下載與 preview 不將 caller Range 傳送至來源。"""
    client, store, media = setup_view()
    token = issue(store, preview=preview)
    endpoint = "preview" if preview else "download"
    with client:
        result = client.get(f"/api/media/{token}/{endpoint}", headers={"Range": "bytes=0-1"})
    assert result.status_code == 200
    assert result.content == b"\xff\xd8\xff\xe0original-image"
    assert result.headers["content-disposition"].startswith("inline;" if preview else "attachment;")
    assert media.calls == [None]


def test_view_rejects_image_range_before_fetch() -> None:
    """圖片 Range 不可進入影片定位路徑。"""
    client, store, media = setup_view()
    token = issue(store)
    with client:
        result = client.get(f"/api/media/{token}/view", headers={"Range": "bytes=0-1"})
    assert result.status_code == 416
    assert result.json()["code"] == "view_range_not_satisfiable"
    assert media.calls == []


@pytest.mark.parametrize("range_header", ["bytes=0-23", "bytes=0-1", "bytes=24-27", "bytes=-3"])
def test_view_video_ranges(range_header: str) -> None:
    """短探測、中段與尾端皆輸出精確原始片段且完成清理。"""
    client, store, media = setup_view()
    token = issue(store, video=True)
    with client:
        result = client.get(f"/api/media/{token}/view", headers={"Range": range_header})
    assert result.status_code == 206
    expected = {
        "bytes=0-23": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        "bytes=0-1": b"\x00\x00",
        "bytes=24-27": b"xxxx",
        "bytes=-3": b"xxx",
    }
    assert result.content == expected[range_header]
    assert result.headers["accept-ranges"] == "bytes"
    assert all(writer.closed for writer in media.writers)


def test_view_probe_is_bounded_and_not_part_of_fragment() -> None:
    """有界 probe 的 bytes 不被混入 Range，且來源未被完整讀取。"""
    media = ScriptedViewClient(
        [
            (
                200,
                {"content-type": "video/mp4", "content-length": "1048"},
                b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"x" * 1024,
            ),
            (
                206,
                {
                    "content-type": "video/mp4",
                    "content-length": "3",
                    "content-range": "bytes 1045-1047/1048",
                },
                b"END",
            ),
        ]
    )
    client, store, _ = setup_view(media)
    token = issue(store, video=True)
    with client:
        result = client.get(f"/api/media/{token}/view", headers={"Range": "bytes=-3"})
    assert result.content == b"END"
    assert media.calls == [None, "bytes=-3"]
    remaining = asyncio.run(media.readers[0].read())
    assert 536 <= len(remaining) < 1048
    assert all(writer.closed for writer in media.writers)


@pytest.mark.parametrize("failure", ["signature", "mime", "total", "budget"])
def test_view_probe_or_fragment_failure_cleans_up(failure: str) -> None:
    """探測驗證、片段一致性與累計 byte budget 不可被小 Range 繞過。"""
    body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4988
    probe = b"<html>unsafe" if failure == "signature" else body
    mime = "video/webm" if failure == "mime" else "video/mp4"
    total = 4999 if failure == "total" else 5000
    media = ScriptedViewClient(
        [
            (200, {"content-type": "video/mp4", "content-length": str(len(probe))}, probe),
            (
                206,
                {
                    "content-type": mime,
                    "content-length": "4998" if failure == "budget" else "2",
                    "content-range": "bytes 2-4999/5000"
                    if failure == "budget"
                    else f"bytes 2-3/{total}",
                },
                body[2:] if failure == "budget" else b"xx",
            ),
        ]
    )
    client, store, _ = setup_view(media)
    token = issue(store, video=True)
    with client:
        result = client.get(
            f"/api/media/{token}/view",
            headers={
                "Range": "bytes=2-" if failure == "budget" else "bytes=2-3",
            },
        )
    assert result.status_code == 502
    assert result.json()["code"] == "upstream_media_invalid"
    assert len(media.calls) == (1 if failure == "signature" else 2)
    assert all(writer.closed for writer in media.writers)


class WaitingProbeClient(ViewClient):
    """讓 probe body 等待，供驗證完整期限與 caller cancellation。"""

    def __init__(self) -> None:
        """建立進入 probe 的同步點。"""
        super().__init__()
        self.started = asyncio.Event()
        self.readers: list[asyncio.StreamReader] = []

    async def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        view_range: ViewRange | None = None,
    ) -> MediaResponse:
        """傳回不會完成首段的受控 response，不啟動外部網路。"""
        self.calls.append(view_range.header if view_range else None)
        writer = FakeResponseWriter()
        self.writers.append(writer)
        started = self.started

        class WaitingReader(asyncio.StreamReader):
            """將同步點放在真實 probe body read，而非 fetch 回傳前。"""

            async def read(self, n: int = -1) -> bytes:
                """確認 probe 已持有 response，然後等待可取消的來源讀取。"""
                started.set()
                return await super().read(n)

        reader = WaitingReader()
        self.readers.append(reader)
        return MediaResponse(
            200,
            {"content-type": "video/mp4", "content-length": "100"},
            reader,
            writer,
            max_bytes=5000,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_view_probe_timeout_or_cancel_stops_before_range(cancel: bool) -> None:
    """Probe 卡住時使用同一完整期限及 lease，失敗後不啟動片段。"""
    media = WaitingProbeClient()
    lease = make_counting_lease()
    task = asyncio.create_task(
        _stream_view(
            make_download_record(),
            media,
            requested=ViewRange(2, 3),
            lease=lease,
            response_timeout=5.0 if cancel else 0.02,
            max_bytes=5000,
        )
    )
    await media.started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AppError) as error:
            await task
        assert error.value.code == "upstream_media_invalid"
    assert media.calls == [None]
    assert all(writer.closed for writer in media.writers)
    assert lease.release_calls == 1
    replacement = lease.limiter.acquire_download("test-client")
    await replacement.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("body_ready", [False, True])
async def test_view_http_disconnect_during_probe_never_starts_range(
    monkeypatch: pytest.MonkeyPatch,
    body_ready: bool,
) -> None:
    """完整 ASGI client disconnect 取消預檢，不等到送出 headers 才觀察。"""
    media = WaitingProbeClient()
    client, store, _ = setup_view(media)
    token = issue(store, video=True)
    scope = make_asgi_scope(spec_version="2.4")
    scope["path"] = f"/api/media/{token}/view"
    scope["raw_path"] = scope["path"].encode()
    scope["headers"] = [(b"range", b"bytes=2-3")]
    queue: asyncio.Queue[Message] = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    messages: list[Message] = []
    leases: list[CountingLease] = []
    original_acquire = client.app.state.limiter.acquire_download

    def acquire(client_ip: str) -> CountingLease:
        """保留真正 limiter 副作用，僅觀察唯一 lease 的釋放次數。"""
        lease = CountingLease(original_acquire(client_ip))
        leases.append(lease)
        return lease

    async def receive() -> Message:
        """提供初始 request 與真正 HTTP disconnect，而非取消 route task。"""
        return await queue.get()

    async def send(message: Message) -> None:
        """觀察完整應用的 ASGI 輸出，不替換 response lifecycle。"""
        messages.append(message)

    monkeypatch.setattr(client.app.state.limiter, "acquire_download", acquire)
    task = asyncio.create_task(client.app(scope, receive, send))
    await asyncio.wait_for(media.started.wait(), timeout=1)
    queue.put_nowait({"type": "http.disconnect"})
    if body_ready:
        media.readers[0].feed_data(b"\x00\x00\x00\x18ftypmp42")
    result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    assert media.calls == [None]
    assert isinstance(result[0], ClientDisconnect)
    assert all(writer.closed for writer in media.writers)
    assert len(leases) == 1 and leases[0].release_calls == 1
    replacement = original_acquire("203.0.113.10")
    await replacement.release()


@pytest.mark.parametrize("limit", ["window", "global", "client"])
def test_view_uses_existing_media_limits(limit: str) -> None:
    """檢視使用相同請求 window 與全域／client slots，不額外排隊。"""
    client, store, media = setup_view()
    token = issue(store)
    leases = []
    if limit == "window":
        client.app.state.attempt_limiter.media_limit = 1
    else:
        for index in range(4 if limit == "global" else 2):
            leases.append(
                client.app.state.limiter.acquire_download(
                    f"peer-{index}" if limit == "global" else "testclient"
                )
            )
    with client:
        if limit == "window":
            assert client.get(f"/api/media/{token}/view").status_code == 200
        result = client.get(f"/api/media/{token}/view")
    assert result.status_code == 429
    assert int(result.headers["retry-after"]) >= 1
    assert len(media.calls) == (1 if limit == "window" else 0)
    for lease in leases:
        asyncio.run(lease.release())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "disconnect", "send_error", "deadline", "cancel"])
async def test_view_response_lifecycle_releases_lease(mode: str) -> None:
    """真實 ASGI 串流所有終止路徑皆關閉來源並釋放唯一 lease。"""
    media = ViewClient()
    lease = make_counting_lease()
    response = await _stream_view(
        make_download_record(),
        media,
        requested=None,
        lease=lease,
        response_timeout=0.05 if mode == "deadline" else 5.0,
        max_bytes=5000,
    )
    messages: list[Message] = []

    async def receive() -> Message:
        """ASGI 2.4 路徑不另外消耗 request body。"""
        raise AssertionError("unexpected receive")

    async def send(message: Message) -> None:
        """在 body 邊界注入明確的 client、send 或取消事件。"""
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            if mode == "disconnect":
                raise OSError("disconnected")
            if mode == "send_error":
                raise RuntimeError("private sentinel")
            if mode == "deadline":
                await asyncio.Event().wait()
            if mode == "cancel":
                raise asyncio.CancelledError()

    if mode == "success":
        await response(make_asgi_scope(spec_version="2.4"), receive, send)
    else:
        expected = (
            asyncio.CancelledError
            if mode == "cancel"
            else (ClientDisconnect if mode == "disconnect" else StreamAborted)
        )
        with pytest.raises(expected):
            await response(make_asgi_scope(spec_version="2.4"), receive, send)
        assert not any(
            m["type"] == "http.response.body" and not m.get("more_body") for m in messages
        )
    assert lease.release_calls == 1
    assert all(writer.closed for writer in media.writers)


@pytest.mark.asyncio
async def test_view_close_failure_preserves_primary_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證失敗後 cleanup error 不蓋過 primary，且 lease 不被佔住。"""
    original_close = MediaResponse.close
    close_calls = 0

    async def fail_close(response: MediaResponse) -> None:
        """先關閉真正 response，再模擬可觀測的 cleanup failure。"""
        nonlocal close_calls
        close_calls += 1
        await original_close(response)
        raise RuntimeError("private cleanup sentinel")

    monkeypatch.setattr(MediaResponse, "close", fail_close)
    media = ScriptedViewClient(
        [(200, {"content-type": "video/mp4", "content-length": "6"}, b"<html>")]
    )
    lease = make_counting_lease()
    with pytest.raises(AppError) as error:
        await _stream_view(
            make_download_record(),
            media,
            requested=None,
            lease=lease,
            response_timeout=5.0,
            max_bytes=5000,
        )
    assert error.value.code == "upstream_media_invalid"
    assert "sentinel" not in error.value.message
    assert close_calls == lease.release_calls == 1
    assert media.writers[0].closed
