"""精確檢查送出狀態、主錯誤、清理與取消的優先順序。"""

import asyncio
from typing import Any

import pytest

from sns_media_list.api.routes import DeadlineStreamingResponse
from sns_media_list.errors import AppError


@pytest.mark.parametrize("stage", ["start", "body", "last", "cleanup", "read"])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_response_failure_state(stage: str, spec: str) -> None:
    """只有已開始未完成的回應中止，且 cleanup 不覆蓋主錯誤。"""
    primary = AppError("upstream_media_invalid", "private primary sentinel")
    secondary = RuntimeError("private cleanup sentinel")
    sent = []
    errors = []
    completed = []
    closes = []

    async def body():
        """提供首段並可於下一次 read 拋出主錯誤。"""
        yield b"first"
        if stage == "read":
            raise primary

    async def send(message):
        """只記錄成功送出的 ASGI 訊息。"""
        current = (
            "start"
            if message["type"] == "http.response.start"
            else ("body" if message.get("more_body") else "last")
        )
        if current == stage:
            raise primary
        sent.append(message)

    async def receive():
        """保持連線，避免以斷線掩蓋失敗。"""
        await asyncio.Event().wait()

    async def cleanup():
        """清理只執行一次並產生次要錯誤。"""
        closes.append(True)
        raise secondary

    response = DeadlineStreamingResponse(
        body(),
        deadline=None,
        cleanup=cleanup,
        on_stream_error=errors.append,
        on_stream_complete=lambda: completed.append(True),
        headers={"Content-Length": "5"},
    )
    scope: dict[str, Any] = {"type": "http", "asgi": {"spec_version": spec}}
    if stage == "cleanup":
        await response(scope, receive, send)
    elif stage == "start":
        with pytest.raises(AppError) as raised:
            await response(scope, receive, send)
        assert raised.value is primary
    else:
        with pytest.raises(RuntimeError, match="Media stream aborted") as raised:
            await response(scope, receive, send)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
    await response._run_cleanup()
    assert closes == [True]
    assert errors == [secondary if stage == "cleanup" else primary]
    assert completed == []
    assert sum(m["type"] == "http.response.start" for m in sent) == (stage != "start")
    if sent:
        assert (b"content-length", b"5") in sent[0]["headers"]
    assert sum(m["type"] == "http.response.body" and not m.get("more_body") for m in sent) == (
        stage == "cleanup"
    )


@pytest.mark.parametrize("stage", ["read", "cleanup"])
@pytest.mark.parametrize("deadline_enabled", [False, True])
async def test_caller_cancellation_identity(stage: str, deadline_enabled: bool) -> None:
    """caller cancellation 的原始例外須在唯一清理與 terminal callback 後傳遞。"""
    cancelled = asyncio.CancelledError("caller marker")
    closes = []
    errors = []

    async def body():
        """先產生 body，必要時傳遞 caller cancellation。"""
        yield b"first"
        if stage == "read":
            raise cancelled

    async def cleanup():
        """記錄 cleanup 並可模擬清理期間取消。"""
        closes.append(True)
        if stage == "cleanup":
            raise cancelled

    async def send(_message):
        """成功接受 ASGI 訊息。"""

    async def receive():
        """ASGI 2.4 不使用 disconnect listener。"""
        raise AssertionError

    deadline = asyncio.get_running_loop().time() + 10 if deadline_enabled else None
    response = DeadlineStreamingResponse(
        body(), deadline=deadline, cleanup=cleanup, on_stream_error=errors.append
    )
    with pytest.raises(asyncio.CancelledError) as raised:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert raised.value is cancelled
    assert closes == [True]
    assert errors == [cancelled]


async def test_deadline_before_start_propagates_safe_error() -> None:
    """deadline 若在 response start 成功前到期，不得正常返回空回應。"""
    errors = []
    closes = []

    async def send(_message):
        """模擬 start 尚未成功送出的等待。"""
        await asyncio.Event().wait()

    async def receive():
        """保持連線不主動斷線。"""
        await asyncio.Event().wait()

    async def cleanup():
        """記錄唯一清理。"""
        closes.append(True)

    response = DeadlineStreamingResponse(
        [b"body"],
        deadline=asyncio.get_running_loop().time(),
        cleanup=cleanup,
        on_stream_error=errors.append,
    )
    with pytest.raises(AppError, match="deadline"):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closes == [True]
    assert len(errors) == 1
