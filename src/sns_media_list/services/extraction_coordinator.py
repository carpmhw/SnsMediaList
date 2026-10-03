"""REST／MCP 共用的 process-local extraction execution boundary。"""

from typing import Protocol

from ..api.limits import RequestLimiter
from ..models import ExtractionResponse


class ExtractionOperation(Protocol):
    """可注入的單筆擷取操作，不依賴 HTTP 或特定 extractor。"""

    async def extract(self, url: str) -> ExtractionResponse:
        """擷取並回傳公開正規化 metadata。"""
        ...


class ExtractionCoordinator:
    """以共享 limiter 管理擷取 lease，不處理 transport 或 attempts。"""

    def __init__(self, service: ExtractionOperation, *, limiter: RequestLimiter) -> None:
        """保存同一 app 的 service 與 process-wide limiter。"""
        self.service = service
        self.limiter = limiter

    async def execute(self, url: str, client_identity: str) -> ExtractionResponse:
        """立即取得 slot，並在成功、例外或取消時釋放。"""
        async with self.limiter.acquire_extraction(client_identity):
            return await self.service.extract(url)
