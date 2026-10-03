"""MCP 測試共用的合成 extractor 與 process-local service。"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from time import monotonic
from typing import cast

import uvicorn
from fastapi import FastAPI

from sns_media_list.config import Settings
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.security.tokens import TokenStore
from sns_media_list.services.extraction_service import ExtractionService
from sns_media_list.url_validation import ValidatedExtractionTarget

X_URL = "https://x.com/example/status/123/"
PRIVATE_SOURCE = "FAKE_PRIVATE_SOURCE_SENTINEL"


class FakeExtractor:
    """提供正規化 gallery records，可同步阻擋工作或注入安全測試例外。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        media_types: tuple[str, ...] = ("image",),
        preview: bool = True,
        blocking: bool = False,
    ) -> None:
        """建立合成 media 與併發同步事件。"""
        self.error = error
        self.media_types = media_types
        self.preview = preview
        self.blocking = blocking
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    async def extract(self, target: ValidatedExtractionTarget) -> list[dict[str, object]]:
        """不建立外部連線，依 validated target 回傳合成主要媒體。"""
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        try:
            if self.blocking:
                await self.release.wait()
            if self.error:
                raise self.error
            records: list[dict[str, object]] = []
            types = self.media_types[:1] if target.kind == "story" else self.media_types
            for index, media_type in enumerate(types, 1):
                extension = "jpg" if media_type == "image" else "mp4"
                host = "pbs.twimg.com" if target.platform == "x" else "scontent.cdninstagram.com"
                records.append(
                    {
                        "platform": target.platform,
                        "post_url": target.canonical_url,
                        "post_id": target.target_id,
                        "author": "example",
                        "num": index,
                        "type": media_type,
                        "extension": extension,
                        "progressive": True,
                        "url": f"https://{host}/media/{PRIVATE_SOURCE}-{index}.{extension}",
                        "preview_url": f"https://{host}/media/{PRIVATE_SOURCE}-{index}.jpg"
                        if self.preview
                        else None,
                        "width": 1200,
                        "height": 800,
                    }
                )
            return records
        finally:
            self.active -= 1


def make_service(
    *,
    settings: Settings | None = None,
    extractor: FakeExtractor | None = None,
    capacity: int = 200,
    clock: Callable[[], float] | None = None,
) -> tuple[ExtractionService, FakeExtractor]:
    """建立正式 service 搭配 fake extractor、受限 token store 與可注入 clock。"""
    settings = settings or Settings()
    extractor = extractor or FakeExtractor()
    store = TokenStore(
        capacity=capacity, ttl_seconds=settings.token_ttl_seconds, clock=clock or monotonic
    )
    return ExtractionService(settings, extractor=extractor, token_store=store), extractor


class FakeWriter:
    """只提供 MediaResponse 所需的有界清理介面。"""

    def __init__(self) -> None:
        """記錄 upstream connection 是否已關閉。"""
        self.closed = False

    def close(self) -> None:
        """記錄 close，不建立真實 socket。"""
        self.closed = True

    async def wait_closed(self) -> None:
        """模擬已完成的 connection cleanup。"""
        return None


class FakeMediaClient:
    """為 token API 提供合法 JPEG response，不連線外部 CDN。"""

    body = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"

    def __init__(self) -> None:
        """保存每次 fetch 的安全性觀測與 writer 清理狀態。"""
        self.headers: list[dict[str, str]] = []
        self.writers: list[FakeWriter] = []

    async def fetch(self, _url: str, *, headers: Mapping[str, str]) -> MediaResponse:
        """回傳有 length 與 MIME 的受限 MediaResponse。"""
        self.headers.append(dict(headers))
        reader = asyncio.StreamReader()
        reader.feed_data(self.body)
        reader.feed_eof()
        writer = FakeWriter()
        self.writers.append(writer)
        return MediaResponse(
            200,
            {"content-type": "image/jpeg", "content-length": str(len(self.body))},
            reader,
            cast(asyncio.StreamWriter, writer),
            max_bytes=1024,
        )


class LocalUvicorn(uvicorn.Server):
    """測試專用 Uvicorn，避免覆寫 pytest 主程序的 signal handlers。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """signal handling 由測試程序擁有，不註冊真實 handlers。"""
        yield


@asynccontextmanager
async def running_app(app: FastAPI) -> AsyncIterator[str]:
    """以保留的動態 loopback socket 啟動真實 HTTP，離開時有界停止。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = LocalUvicorn(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            access_log=False,
            log_level="critical",
            ws="none",
            proxy_headers=False,
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("local server did not start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await task
        finally:
            listener.close()
