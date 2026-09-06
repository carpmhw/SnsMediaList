"""真實 Uvicorn 測試專用的 deterministic 上游注入。"""

import asyncio
import os
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from sns_media_list.api import routes
from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.network.media_client import MediaResponse
from sns_media_list.security.tokens import TokenStore
from sns_media_list.services.extraction_service import ExtractionService

SENTINEL = "PRIVATE_SENTINEL_URL_TOKEN_COOKIE_AUTH_ETAG"
IMAGE = b"\xff\xd8\xff" + b"image-data" * 10000
VIDEO = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"video-data" * 10000


class Extractor:
    """提供不接觸外部服務的 Instagram metadata。"""

    async def extract(self, _url: Any) -> list[dict[str, Any]]:
        """依測試設定回傳圖片或 progressive 影片。"""
        kind = os.environ.get("MEDIA_KIND", "image")
        return [
            {
                "platform": "instagram",
                "post_url": "https://www.instagram.com/p/fixture/",
                "post_id": "fixture",
                "num": 1,
                "type": kind,
                "url": f"https://scontent.cdninstagram.com/{SENTINEL}?token={SENTINEL}",
                "extension": "jpg" if kind == "image" else "mp4",
                "progressive": True,
            }
        ]


class Reader:
    """在首段後模擬不同傳輸結果。"""

    def __init__(self, body: bytes, mode: str) -> None:
        """保存媒體內容與失敗模式。"""
        self.body = body
        self.mode = mode
        self.reads = 0

    async def read(self, size: int) -> bytes:
        """首段正常，後續可截斷、洩密例外或 read timeout。"""
        self.reads += 1
        if self.reads > 1:
            if self.mode == "truncated":
                return b""
            if self.mode == "error":
                raise OSError(SENTINEL)
            if self.mode in {"timeout", "disconnect"}:
                await asyncio.Event().wait()
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class Writer:
    """追蹤 upstream connection 的 close 次數。"""

    def __init__(self, client: "Client") -> None:
        """保存共享統計。"""
        self.client = client

    def close(self) -> None:
        """記錄一次 upstream close。"""
        self.client.closes += 1

    async def wait_closed(self) -> None:
        """立即完成 fixture cleanup。"""


class Client:
    """注入真實 MediaResponse，保留 validation 與讀取限制。"""

    def __init__(self) -> None:
        """初始化可觀測的上游統計。"""
        self.fetches = 0
        self.closes = 0
        self.ranges: list[int | None] = []
        self.methods: list[str] = []
        self.read_timeout = 0.2

    async def fetch(self, _url: str, *, headers: Any, range_start: int | None = None) -> Any:
        """依環境配置建立固定長度或 EOF-delimited 上游。"""
        self.fetches += 1
        self.ranges.append(range_start)
        kind = os.environ.get("MEDIA_KIND", "image")
        body = IMAGE if kind == "image" else VIDEO
        mode = os.environ.get("STREAM_MODE", "success")
        response_headers = {"content-type": "image/jpeg" if kind == "image" else "video/mp4"}
        if os.environ.get("KNOWN_LENGTH", "1") == "1":
            response_headers["content-length"] = str(len(body))
        return MediaResponse(
            403 if mode == "reject" else 200,
            response_headers,
            Reader(body, mode),
            Writer(self),
            max_bytes=1000000,
            read_timeout=10 if mode == "disconnect" else self.read_timeout,
        )


def factory() -> Any:
    """使用正式 app factory，僅替換外部服務並新增測試統計路由。"""
    settings = Settings(generated_previews_enabled=False).model_copy(
        update={"extraction_proxy_port": 0}
    )
    service = ExtractionService(
        settings,
        extractor=Extractor(),
        token_store=TokenStore(capacity=100, ttl_seconds=300),
    )
    client = Client()
    app = create_app(settings=settings, extraction_service=service, media_client=client)
    app.add_middleware(DownloadObservationMiddleware, client=client)
    if os.environ.get("STREAM_MODE") == "postcomplete":
        original_stream = routes._stream_media

        async def stream_with_late_cleanup(*args: Any, **kwargs: Any) -> Any:
            """只在最後 body 完成後的 response cleanup 注入失敗。"""
            response = await original_stream(*args, **kwargs)
            original_cleanup = response._cleanup

            async def late_cleanup() -> None:
                """保留原始冪等清理後產生不可外洩的次要錯誤。"""
                await original_cleanup()
                raise RuntimeError(SENTINEL)

            response._cleanup = late_cleanup
            return response

        routes._stream_media = stream_with_late_cleanup
    if os.environ.get("STREAM_MODE") == "sendfailure":
        app.add_middleware(SendFailureMiddleware)

    async def stats() -> dict[str, Any]:
        """提供測試程序核對 cleanup 與 resume 的安全計數。"""
        return {
            "fetches": client.fetches,
            "closes": client.closes,
            "ranges": client.ranges,
            "active": app.state.limiter._active_downloads,
            "methods": client.methods,
        }

    # 靜態 mount 前加入測試路由，不更動正式應用路由。
    async def stats_response(_request: Any) -> Any:
        """將共享統計包裝成測試 JSON 回應。"""
        return JSONResponse(await stats())

    app.router.routes.insert(0, Route("/fixture-stats", stats_response))
    return app


class DownloadObservationMiddleware:
    """只記錄下載 method，不保存 token path 或 access log。"""

    def __init__(self, app: Any, client: Client) -> None:
        """保存測試計數與下層應用。"""
        self.app = app
        self.client = client

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """在伺服器入口觀察真正收到的 HEAD 與 GET。"""
        if scope.get("path", "").endswith("/download"):
            self.client.methods.append(scope["method"])
        await self.app(scope, receive, send)


class SendFailureMiddleware:
    """在真實 server 中注入 deterministic downstream send failure。"""

    def __init__(self, app: Any) -> None:
        """保存下層 ASGI application。"""
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """下載首段成功後，在下一段送出前模擬 transport 失敗。"""
        count = 0

        async def failing_send(message: Any) -> None:
            """只對下載的第二段 body 注入敏感例外。"""
            nonlocal count
            if scope.get("path", "").endswith("/download") and message.get("body"):
                count += 1
                if count == 2:
                    raise RuntimeError(SENTINEL)
            await send(message)

        await self.app(scope, receive, failing_send)
