"""Tests for extractor egress destination policy and CONNECT parsing."""

import asyncio
import gc
import ipaddress
import threading
import time
from typing import Any

import pytest

from sns_media_list.app import _EXTRACTION_HOSTS
from sns_media_list.errors import AppError
from sns_media_list.network import connect_proxy
from sns_media_list.network.connect_proxy import ConnectProxy, _relay, parse_connect_request
from sns_media_list.network.dns import DestinationPolicy


def test_x_authenticated_client_asset_host_is_allowlisted() -> None:
    """Verify authenticated X extraction can load its client transaction asset."""
    assert "abs.twimg.com" in _EXTRACTION_HOSTS


class FakeStreamReader:
    """Provide the minimal async reader surface used by the proxy test."""

    def __init__(self, request: bytes = b"") -> None:
        """Store one request and then return EOF for relay reads."""
        self.request = request

    async def readuntil(self, _separator: bytes) -> bytes:
        """Return the configured request headers."""
        return self.request

    async def read(self, _size: int) -> bytes:
        """Return EOF so the fake tunnel closes immediately."""
        return b""


class FakeStreamWriter:
    """Provide the minimal async writer surface used by the proxy test."""

    def __init__(self) -> None:
        """Initialize captured writes and close state."""
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        """Capture bytes written by the proxy."""
        self.writes.append(value)

    async def drain(self) -> None:
        """Complete a fake write without blocking."""

    def close(self) -> None:
        """Close the fake stream."""

    async def wait_closed(self) -> None:
        """Complete fake close cleanup."""


class TrackingStreamWriter(FakeStreamWriter):
    """記錄 proxy 是否關閉了 tunnel 兩側的 writer。"""

    def __init__(self) -> None:
        """初始化 close 追蹤狀態。"""
        super().__init__()
        self.closed = False
        self.closed_event = asyncio.Event()
        self.wait_closed_called = False

    def close(self) -> None:
        """記錄 writer close 呼叫。"""
        self.closed = True
        self.closed_event.set()

    async def wait_closed(self) -> None:
        """記錄 writer wait_closed 呼叫。"""
        self.wait_closed_called = True


class FailingWaitClosedWriter(TrackingStreamWriter):
    """模擬 upstream wait_closed 立即回報 socket cleanup 例外。"""

    async def wait_closed(self) -> None:
        """記錄等待後拋出不應阻塞另一側 cleanup 的 OSError。"""
        self.wait_closed_called = True
        raise OSError("private upstream cleanup detail")


class CancellationResistantWaitClosedWriter(TrackingStreamWriter):
    """模擬取消後仍等待釋放訊號的 upstream writer cleanup。"""

    def __init__(self) -> None:
        """初始化 cancellation-resistant wait_closed 的生命週期事件。"""
        super().__init__()
        self.wait_started = asyncio.Event()
        self.wait_cancelled = asyncio.Event()
        self.release_wait = asyncio.Event()

    async def wait_closed(self) -> None:
        """等待釋放訊號，並在收到取消後繼續等待以模擬 hanging cleanup。"""
        self.wait_closed_called = True
        self.wait_started.set()
        try:
            await self.release_wait.wait()
        except asyncio.CancelledError:
            self.wait_cancelled.set()
            await self.release_wait.wait()


class BlockingStreamReader(FakeStreamReader):
    """Keep a proxy handler suspended until shutdown cancellation."""

    async def readuntil(self, _separator: bytes) -> bytes:
        """Block forever so the test can exercise active-handler cleanup."""
        await asyncio.Event().wait()
        return b""


class CancellationResistantRelayReader(FakeStreamReader):
    """模擬取消後仍等待釋放訊號的 relay reader。"""

    def __init__(self) -> None:
        """初始化 relay reader 的取消生命週期事件。"""
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        """等待釋放訊號，並記錄 copy task 收到取消。"""
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        finally:
            self.finished.set()
        return b""


class LateUpstreamWriter(TrackingStreamWriter):
    """模擬 timeout 後才取得且 wait_closed 可能長時間等待的 upstream writer。"""

    def __init__(self) -> None:
        """初始化 late writer 的 bounded cleanup 追蹤事件。"""
        super().__init__()
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.wait_finished = asyncio.Event()

    async def wait_closed(self) -> None:
        """等待外部釋放，模擬 cancellation-resistant socket cleanup。"""
        self.wait_closed_called = True
        self.wait_started.set()
        try:
            await self.release_wait.wait()
        finally:
            self.wait_finished.set()


@pytest.mark.asyncio
async def test_relay_cancels_opposite_copy_after_one_side_eof() -> None:
    """一側 EOF 時 relay 應在 bounded 時間返回並取消另一側 copy。"""
    client_reader = FakeStreamReader()
    client_writer = FakeStreamWriter()
    upstream_reader = CancellationResistantRelayReader()
    upstream_writer = FakeStreamWriter()
    relay_task = asyncio.create_task(
        _relay(client_reader, client_writer, upstream_reader, upstream_writer)
    )

    try:
        await asyncio.wait_for(upstream_reader.started.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({relay_task}, timeout=0.2)
        assert relay_task in done
        assert upstream_reader.cancelled.is_set()
    finally:
        upstream_reader.release.set()
        if not upstream_reader.finished.is_set():
            await asyncio.wait_for(upstream_reader.finished.wait(), timeout=0.2)
        if not relay_task.done():
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_relay_observes_all_simultaneous_copy_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relay 同時完成的雙向 copy 例外都應被觀察。"""
    failures = [RuntimeError("client copy failed"), RuntimeError("upstream copy failed")]

    async def fail_copy(_reader: Any, _writer: Any) -> None:
        """讓兩個 copy task 在同一輪事件迴圈中失敗。"""
        raise failures.pop()

    monkeypatch.setattr(connect_proxy, "_copy_stream", fail_copy)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 relay 觀察的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        with pytest.raises(RuntimeError):
            await _relay(
                FakeStreamReader(),
                FakeStreamWriter(),
                FakeStreamReader(),
                FakeStreamWriter(),
            )
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancel_copy_tasks_observes_done_failure_when_cleanup_is_cancelled() -> None:
    """relay cleanup 被取消時仍應消耗已完成 failed task 並觀察 pending task。"""
    release_pending = asyncio.Event()
    pending_cancelled = asyncio.Event()

    async def fail_copy() -> None:
        """建立尚未被 await 的已完成 copy task 例外。"""
        raise RuntimeError("completed copy failure")

    async def cancellation_resistant_copy() -> None:
        """收到取消後等待釋放，再拋出供 detached observer 消耗的例外。"""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pending_cancelled.set()
            await release_pending.wait()
        raise RuntimeError("pending copy failure")

    completed_task = asyncio.create_task(fail_copy())
    await asyncio.wait({completed_task})
    pending_task = asyncio.create_task(cancellation_resistant_copy())
    cleanup_task = asyncio.create_task(
        connect_proxy._cancel_copy_tasks({completed_task, pending_task})
    )

    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 copy cleanup observer 消耗的 task 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        await asyncio.wait_for(pending_cancelled.wait(), timeout=0.1)
        cleanup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task
        release_pending.set()
        with pytest.raises(RuntimeError, match="pending copy failure"):
            await asyncio.wait_for(asyncio.shield(pending_task), timeout=0.1)
        del completed_task
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        release_pending.set()
        if not pending_task.done():
            pending_task.cancel()
        await asyncio.gather(pending_task, return_exceptions=True)
        if not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


def test_policy_selects_a_validated_public_address() -> None:
    """Verify a permitted host is pinned to one validated resolver result."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )

    target = policy.validate("www.instagram.com", 443)

    assert target.hostname == "www.instagram.com"
    assert str(target.address) == "93.184.216.34"


@pytest.mark.asyncio
async def test_proxy_policy_validation_is_threaded_and_deadline_bounded() -> None:
    """同步 DNS validation 應移出 event loop，逾時後 late 例外也必須被觀察。"""
    main_thread_id = threading.get_ident()
    resolver_started = threading.Event()
    resolver_finished = threading.Event()
    resolver_thread_ids: list[int] = []
    resolver_started_at: list[float] = []
    event_loop_ticks: list[float] = []

    def slow_resolver(
        _host: str,
        _port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """在 worker thread 延遲後拋出安全例外，模擬慢速系統 resolver。"""
        resolver_thread_ids.append(threading.get_ident())
        resolver_started_at.append(time.monotonic())
        resolver_started.set()
        time.sleep(0.15)
        resolver_finished.set()
        raise AppError("unsafe_destination", "late resolver failure")

    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=slow_resolver,
    )
    proxy = ConnectProxy(policy, operation_timeout_seconds=0.02)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = FakeStreamWriter()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄未被 proxy validation observer 消耗的背景例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    handler = asyncio.create_task(proxy.handle_client(client, client_writer))
    loop.call_later(0.005, lambda: event_loop_ticks.append(time.monotonic()))

    try:
        assert await asyncio.wait_for(asyncio.to_thread(resolver_started.wait, 0.5), timeout=0.5)
        done, _pending = await asyncio.wait({handler}, timeout=0.1)
        assert handler in done
        assert handler.result() is None
        assert resolver_thread_ids != [main_thread_id]
        assert event_loop_ticks
        assert event_loop_ticks[0] - resolver_started_at[0] < 0.1
    finally:
        assert await asyncio.to_thread(resolver_finished.wait, 0.5)
        await asyncio.sleep(0.01)
        gc.collect()
        await asyncio.sleep(0)
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        loop.set_exception_handler(previous_handler)

    assert unhandled == []


def test_policy_rejects_deceptive_host() -> None:
    """Verify an allowed hostname suffix cannot be faked by another host."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate("www.instagram.com.evil.example", 443)

    assert exc_info.value.code == "unsafe_destination"


def test_cross_host_redirect_requires_a_new_allowlist_check() -> None:
    """Verify a redirect target cannot reuse the source host authorization."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    policy.validate("www.instagram.com", 443)

    with pytest.raises(AppError) as exc_info:
        policy.validate("video.external.example", 443)

    assert exc_info.value.code == "unsafe_destination"


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "::1", "fc00::1", "2001:db8::1"]
)
def test_policy_rejects_any_non_public_dns_answer(address: str) -> None:
    """Verify one unsafe A or AAAA answer rejects the complete destination."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address(address),
        ],
    )

    with pytest.raises(AppError) as exc_info:
        policy.validate("www.instagram.com", 443)

    assert exc_info.value.code == "unsafe_destination"


def test_connect_request_requires_https_port() -> None:
    """Verify only CONNECT host:443 requests are accepted by the proxy parser."""
    host, port = parse_connect_request(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )

    assert host == "www.instagram.com"
    assert port == 443


@pytest.mark.asyncio
async def test_proxy_connects_to_selected_pinned_address(monkeypatch: Any) -> None:
    """Verify the proxy connects to the validated IP rather than re-resolving."""
    from sns_media_list.network import connect_proxy

    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = connect_proxy.ConnectProxy(policy)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = FakeStreamWriter()
    captured: dict[str, object] = {}

    async def fake_open_connection(
        host: str, port: int
    ) -> tuple[FakeStreamReader, FakeStreamWriter]:
        """Capture the pinned destination requested by the proxy."""
        captured.update({"host": host, "port": port})
        return FakeStreamReader(), FakeStreamWriter()

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)

    await proxy.handle_client(client, client_writer)

    assert captured == {"host": "93.184.216.34", "port": 443}
    assert client_writer.writes[0].startswith(b"HTTP/1.1 200")


@pytest.mark.asyncio
async def test_proxy_header_blackhole_returns_within_operation_timeout() -> None:
    """header readuntil blackhole 時 proxy handler 應在 bounded 時間內返回。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy, operation_timeout_seconds=0.03)
    handler = asyncio.create_task(proxy.handle_client(BlockingStreamReader(), FakeStreamWriter()))

    try:
        done, _pending = await asyncio.wait({handler}, timeout=0.2)
        assert handler in done
        assert not handler.cancelled()
    finally:
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_closes_upstream_writer_that_arrives_after_open_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_connection late success 時 observer 應關閉 delayed upstream writer。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy, operation_timeout_seconds=0.03)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = TrackingStreamWriter()
    late_writer = LateUpstreamWriter()
    open_started = asyncio.Event()
    open_cancelled = asyncio.Event()
    release_open = asyncio.Event()

    async def cancellation_resistant_open_connection(
        _host: str, _port: int
    ) -> tuple[FakeStreamReader, LateUpstreamWriter]:
        """忽略取消直到測試釋放，模擬 late upstream connection result。"""
        open_started.set()
        try:
            await release_open.wait()
        except asyncio.CancelledError:
            open_cancelled.set()
            await release_open.wait()
        return FakeStreamReader(), late_writer

    monkeypatch.setattr(
        connect_proxy.asyncio,
        "open_connection",
        cancellation_resistant_open_connection,
    )
    handler = asyncio.create_task(proxy.handle_client(client, client_writer))

    try:
        await asyncio.wait_for(open_started.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({handler}, timeout=0.2)
        assert handler in done
        assert open_cancelled.is_set()
        assert client_writer.writes == [b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"]

        release_open.set()
        await asyncio.wait_for(late_writer.wait_started.wait(), timeout=0.2)
        assert late_writer.closed is True
        assert late_writer.wait_closed_called is True
    finally:
        release_open.set()
        late_writer.release_wait.set()
        await asyncio.sleep(0)
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_relay_timeout_does_not_write_forbidden_after_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relay timeout 發生在 200 後時只能 silent close，不得追加 403。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy, operation_timeout_seconds=0.03)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = TrackingStreamWriter()
    relay_started = asyncio.Event()
    relay_cancelled = asyncio.Event()
    relay_finished = asyncio.Event()
    release_relay = asyncio.Event()

    async def cancellation_resistant_relay(*_args: Any) -> None:
        """忽略取消直到釋放，模擬 relay blackhole。"""
        relay_started.set()
        try:
            await release_relay.wait()
        except asyncio.CancelledError:
            relay_cancelled.set()
            await release_relay.wait()
        finally:
            relay_finished.set()

    async def fake_open_connection(
        _host: str, _port: int
    ) -> tuple[FakeStreamReader, TrackingStreamWriter]:
        """回傳可立即建立 tunnel 的 fake upstream。"""
        return FakeStreamReader(), TrackingStreamWriter()

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(connect_proxy, "_relay", cancellation_resistant_relay)
    handler = asyncio.create_task(proxy.handle_client(client, client_writer))

    try:
        await asyncio.wait_for(relay_started.wait(), timeout=0.1)
        done, _pending = await asyncio.wait({handler}, timeout=0.2)
        assert handler in done
        assert relay_cancelled.is_set()
        assert client_writer.writes == [b"HTTP/1.1 200 Connection Established\r\n\r\n"]
    finally:
        release_relay.set()
        await asyncio.wait_for(relay_finished.wait(), timeout=0.2)
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_does_not_write_forbidden_after_tunnel_is_established(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECT 200 後 relay reset 不得再把 403 寫入已建立 tunnel。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = TrackingStreamWriter()
    upstream_writer = TrackingStreamWriter()

    async def fake_open_connection(
        _host: str, _port: int
    ) -> tuple[FakeStreamReader, TrackingStreamWriter]:
        """回傳可追蹤 cleanup 的上游 tunnel writer。"""
        return FakeStreamReader(), upstream_writer

    async def reset_relay(*_args: Any) -> None:
        """模擬 CONNECT response 送出後的上游 reset。"""
        raise ConnectionResetError("upstream reset")

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(connect_proxy, "_relay", reset_relay)

    await proxy.handle_client(client, client_writer)

    assert client_writer.writes == [b"HTTP/1.1 200 Connection Established\r\n\r\n"]
    assert client_writer.closed is True
    assert client_writer.wait_closed_called is True
    assert upstream_writer.closed is True
    assert upstream_writer.wait_closed_called is True


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_kind", ["oserror", "hanging"])
async def test_proxy_closes_client_when_upstream_cleanup_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
    writer_kind: str,
) -> None:
    """upstream wait_closed 失敗或 hanging 時仍應 bounded 關閉 client writer。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = TrackingStreamWriter()
    upstream_writer: FailingWaitClosedWriter | CancellationResistantWaitClosedWriter
    if writer_kind == "oserror":
        upstream_writer = FailingWaitClosedWriter()
    else:
        upstream_writer = CancellationResistantWaitClosedWriter()

    async def fake_open_connection(
        _host: str, _port: int
    ) -> tuple[FakeStreamReader, TrackingStreamWriter]:
        """回傳受控 upstream writer 以測試兩側獨立 cleanup。"""
        return FakeStreamReader(), upstream_writer

    async def reset_relay(*_args: Any) -> None:
        """模擬 tunnel 建立後立即發生 upstream reset。"""
        raise ConnectionResetError("upstream reset")

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(connect_proxy, "_relay", reset_relay)
    handler = asyncio.create_task(proxy.handle_client(client, client_writer))

    try:
        if isinstance(upstream_writer, CancellationResistantWaitClosedWriter):
            await asyncio.wait_for(upstream_writer.wait_started.wait(), timeout=0.1)
        else:
            await asyncio.sleep(0)
        await asyncio.wait_for(client_writer.closed_event.wait(), timeout=0.3)
        assert client_writer.closed is True
    finally:
        if isinstance(upstream_writer, CancellationResistantWaitClosedWriter):
            upstream_writer.release_wait.set()
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_shutdown_does_not_wait_forever_for_one_writer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """proxy shutdown 不應被單一 cancellation-resistant wait_closed 卡住。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy)
    client = FakeStreamReader(
        b"CONNECT www.instagram.com:443 HTTP/1.1\r\nHost: www.instagram.com\r\n\r\n"
    )
    client_writer = TrackingStreamWriter()
    upstream_writer = CancellationResistantWaitClosedWriter()
    relay_started = asyncio.Event()

    async def fake_open_connection(
        _host: str, _port: int
    ) -> tuple[FakeStreamReader, CancellationResistantWaitClosedWriter]:
        """回傳 cancellation-resistant upstream writer。"""
        return FakeStreamReader(), upstream_writer

    async def blocked_relay(*_args: Any) -> None:
        """保持 tunnel handler active 直到 shutdown 發出取消。"""
        relay_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(connect_proxy.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(connect_proxy, "_relay", blocked_relay)
    handler = asyncio.create_task(proxy.handle_client(client, client_writer))
    shutdown: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(relay_started.wait(), timeout=0.1)
        shutdown = asyncio.create_task(proxy.close_clients())
        done, _pending = await asyncio.wait({shutdown}, timeout=0.3)
        assert shutdown in done
        assert client_writer.closed is True
    finally:
        upstream_writer.release_wait.set()
        if shutdown is not None and not shutdown.done():
            shutdown.cancel()
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_shutdown_cancels_active_client_handlers() -> None:
    """Verify shutdown finishes handlers before the event loop is finalized."""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy)
    task = asyncio.create_task(proxy.handle_client(BlockingStreamReader(), FakeStreamWriter()))
    await asyncio.sleep(0)

    try:
        await proxy.close_clients()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_proxy_close_clients_is_bounded_and_observes_late_handler_failure() -> None:
    """shutdown 遇到 cancellation-resistant active handler 時應 bounded 且觀察 late failure。"""
    policy = DestinationPolicy(
        allowed_hosts=frozenset({"www.instagram.com"}),
        resolver=lambda _host, _port: [ipaddress.ip_address("93.184.216.34")],
    )
    proxy = ConnectProxy(policy)
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    release_handler = asyncio.Event()
    handler_finished = asyncio.Event()

    async def cancellation_resistant_handler() -> None:
        """收到 shutdown cancel 後仍等待釋放，再拋出 late failure。"""
        handler_started.set()
        try:
            await release_handler.wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            await release_handler.wait()
        finally:
            handler_finished.set()
        raise RuntimeError("late active handler failure")

    handler = asyncio.create_task(cancellation_resistant_handler())
    proxy._client_tasks.add(handler)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """記錄 shutdown 後未被 observer 消耗的 handler 例外。"""
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=0.1)
        shutdown = asyncio.create_task(proxy.close_clients())
        done, _pending = await asyncio.wait({shutdown}, timeout=0.2)
        assert shutdown in done
        assert handler_cancelled.is_set()

        release_handler.set()
        await asyncio.wait_for(handler_finished.wait(), timeout=0.2)
        await asyncio.sleep(0)
        proxy._client_tasks.discard(handler)
        del handler
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        release_handler.set()
        if not handler_finished.is_set():
            await asyncio.wait_for(handler_finished.wait(), timeout=0.2)
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize(
    "raw_request",
    [
        b"GET https://www.instagram.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT www.instagram.com:80 HTTP/1.1\r\n\r\n",
    ],
)
def test_connect_request_rejects_unsafe_forms(raw_request: bytes) -> None:
    """Verify malformed and unsafe CONNECT requests fail before networking."""
    with pytest.raises(AppError) as exc_info:
        parse_connect_request(raw_request)

    assert exc_info.value.code == "unsafe_destination"
