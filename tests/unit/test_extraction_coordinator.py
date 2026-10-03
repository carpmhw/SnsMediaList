"""共享 extraction slot 的即時拒絕與取消／錯誤清理契約。"""

import asyncio

import pytest

from sns_media_list.api.limits import RequestLimiter
from sns_media_list.errors import AppError
from sns_media_list.models import ExtractionResponse


class ControlledService:
    """以同步事件控制 extraction，避免測試依賴真實平台或 sleep。"""

    def __init__(self, error: Exception | None = None) -> None:
        """建立工作開始與放行事件。"""
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.error = error
        self.calls = 0

    async def extract(self, url: str) -> ExtractionResponse:
        """等待放行後回傳公開結果，或拋出指定錯誤。"""
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        if self.error:
            raise self.error
        return ExtractionResponse(platform="x", post_url=url, media=[])


@pytest.mark.parametrize(
    "error", [None, AppError("extraction_failed", "safe"), RuntimeError("fake")]
)
async def test_shared_slot_is_released_after_success_or_failure(error: Exception | None) -> None:
    """兩個 coordinator 共用同一 budget，任何終止結果都會釋放 slot。"""
    from sns_media_list.services.extraction_coordinator import ExtractionCoordinator

    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    service = ControlledService(error)
    first = ExtractionCoordinator(service, limiter=limiter)
    second = ExtractionCoordinator(service, limiter=limiter)
    task = asyncio.create_task(first.execute("https://x.com/example/status/1", "rest-client"))
    await service.started.wait()
    with pytest.raises(AppError, match="active") as rejected:
        await second.execute("https://x.com/example/status/1", "mcp")
    assert rejected.value.code == "local_rate_limited"
    assert service.calls == 1
    service.finish.set()
    if error:
        with pytest.raises(type(error)):
            await task
    else:
        await task
    async with limiter.acquire_extraction("mcp"):
        pass


async def test_cancelled_extraction_releases_shared_slot() -> None:
    """取消不會被吞掉，後續 transport 可再次取得 budget。"""
    from sns_media_list.services.extraction_coordinator import ExtractionCoordinator

    service = ControlledService()
    limiter = RequestLimiter(max_extractions=1, max_downloads=1)
    coordinator = ExtractionCoordinator(service, limiter=limiter)
    task = asyncio.create_task(coordinator.execute("https://x.com/example/status/1", "mcp"))
    await service.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with limiter.acquire_extraction("rest-client"):
        pass
