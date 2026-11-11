"""以獨立 Uvicorn 驗證原始檢視的 HTTP framing 與來源清理。"""

import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from tests.integration.download_server import SENTINEL, VIDEO
from tests.integration.test_download_server import events, extract, running_server


@pytest.mark.parametrize("backend", ["h11", "httptools"])
@pytest.mark.parametrize("range_header", [None, "bytes=0-23", "bytes=0-1", "bytes=24-", "bytes=-3"])
def test_real_view_bytes_and_cleanup(
    tmp_path: Path, backend: str, range_header: str | None
) -> None:
    """原始與三種 Range 經真實 HTTP 保留 bytes，且不產生下載完成事件。"""
    with running_server(tmp_path, kind="video", backend=backend) as origin:
        media = extract(origin)
        view = media["download_url"].removesuffix("download") + "view"
        assert httpx.head(origin + view).status_code == 204
        response = httpx.get(origin + view, headers={"Range": range_header} if range_header else {})
        assert response.status_code == (206 if range_header else 200)
        expected = {
            None: VIDEO,
            "bytes=0-23": VIDEO[:24],
            "bytes=0-1": VIDEO[:2],
            "bytes=24-": VIDEO[24:],
            "bytes=-3": VIDEO[-3:],
        }
        assert response.content == expected[range_header]
        assert response.headers["content-disposition"].startswith("inline;")
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats["active"] == 0
        assert (
            stats["fetches"]
            == stats["closes"]
            == (
                2
                if range_header
                in {
                    "bytes=0-1",
                    "bytes=24-",
                    "bytes=-3",
                }
                else 1
            )
        )
    log = (tmp_path / "server.log").read_text()
    assert SENTINEL not in log
    assert not any(event["event"].startswith("media_download_") for event in events(tmp_path))


@pytest.mark.parametrize("mode", ["truncated", "error", "timeout"])
@pytest.mark.parametrize("range_header", [None, "bytes=24-"])
def test_real_view_aborts_incomplete_body(
    tmp_path: Path, mode: str, range_header: str | None
) -> None:
    """送出 headers 後的提前 EOF／錯誤不偽造完整結尾或送第二份 response。"""
    with running_server(tmp_path, kind="video", mode=mode) as origin:
        media = extract(origin)
        view = media["download_url"].removesuffix("download") + "view"
        with pytest.raises(httpx.RemoteProtocolError):
            httpx.get(origin + view, headers={"Range": range_header} if range_header else {})
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats["active"] == 0
        assert stats["fetches"] == stats["closes"] == (2 if range_header else 1)
    log = (tmp_path / "server.log").read_text()
    assert "StreamAborted" in log
    assert SENTINEL not in log
    assert "Caught handled exception" not in log
    assert not any(event["event"].startswith("media_download_") for event in events(tmp_path))


def test_real_view_disconnect_releases_slot(tmp_path: Path) -> None:
    """瀏覽器離開檢視後，真實 server 取消來源讀取並回收 slot。"""
    with running_server(tmp_path, kind="video", mode="disconnect") as origin:
        media = extract(origin)
        view = media["download_url"].removesuffix("download") + "view"
        with httpx.stream("GET", origin + view) as response:
            assert response.status_code == 200
            assert next(response.iter_bytes())
        deadline = time.monotonic() + 3
        while True:
            stats = httpx.get(origin + "/fixture-stats").json()
            if stats["active"] == 0:
                break
            assert time.monotonic() < deadline, stats
            time.sleep(0.02)
        assert stats["fetches"] == stats["closes"] == 1


def test_real_view_disconnect_before_headers_cancels_probe(tmp_path: Path) -> None:
    """真實 TCP client 在 probe 等待時離線，毋須等來源十秒 idle timeout。"""
    with running_server(tmp_path, kind="video", mode="probe-disconnect") as origin:
        media = extract(origin)
        view = media["download_url"].removesuffix("download") + "view"
        target = urlsplit(origin)
        assert target.hostname and target.port
        with socket.create_connection((target.hostname, target.port), timeout=1) as connection:
            connection.sendall(
                f"GET {view} HTTP/1.1\r\nHost: 127.0.0.1\r\nRange: bytes=2-3\r\n\r\n".encode()
            )
            deadline = time.monotonic() + 2
            while True:
                stats = httpx.get(origin + "/fixture-stats").json()
                if stats["fetches"] == 1:
                    break
                assert time.monotonic() < deadline, stats
                time.sleep(0.02)
        deadline = time.monotonic() + 2
        while True:
            stats = httpx.get(origin + "/fixture-stats").json()
            if stats["active"] == 0:
                break
            assert time.monotonic() < deadline, stats
            time.sleep(0.02)
        assert stats["fetches"] == stats["closes"] == 1
        assert stats["ranges"] == [None]
