"""獨立 Uvicorn 日誌測試專用的無外部平台 app factory。"""

import asyncio
from typing import cast

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Route

from sns_media_list.app import create_app
from sns_media_list.config import Settings
from sns_media_list.errors import AppError
from sns_media_list.network.connect_proxy import ConnectProxy
from sns_media_list.url_validation import ValidatedExtractionTarget
from tests.api.test_mcp import FakeProxy
from tests.mcp_helpers import FakeExtractor, make_service


class ScenarioExtractor(FakeExtractor):
    """依合成 target ID 切換成功、AppError、未知例外或可取消工作。"""

    def __init__(self) -> None:
        """建立只供測試同步的 blocking-started event。"""
        super().__init__()
        self.blocking_started = asyncio.Event()

    async def extract(self, target: ValidatedExtractionTarget) -> list[dict[str, object]]:
        """只使用 fixture sentinel，不讀 Cookie 或連線社群平台。"""
        if target.target_id == "2":
            raise AppError("post_unavailable", "SENSITIVE_RAW_APPEARANCE")
        if target.target_id == "3":
            raise RuntimeError("SENSITIVE_RAW_EXCEPTION")
        if target.target_id == "4":
            self.blocking_started.set()
            await self.release.wait()
        return await super().extract(target)


def create_test_app() -> FastAPI:
    """建立正式 MCP／REST app 配合 fake service 與不 bind 的 proxy。"""
    settings = Settings(mcp_enabled=True, instagram_cookie_file=None, x_cookie_file=None)
    extractor = ScenarioExtractor()
    service, _ = make_service(settings=settings, extractor=extractor)
    app = create_app(
        settings=settings,
        extraction_service=service,
        extraction_proxy=cast(ConnectProxy, FakeProxy([])),
    )

    async def extraction_started(_request: object) -> JSONResponse:
        """測試限定同步訊號，不存在於 production factory。"""
        return JSONResponse({"started": extractor.blocking_started.is_set()})

    app.router.routes.insert(0, Route("/_test/extraction_started", endpoint=extraction_started))
    return app
