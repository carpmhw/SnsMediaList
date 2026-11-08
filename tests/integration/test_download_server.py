"""獨立程序的 HTTP framing、安全 stderr 與原生下載驗收。"""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.download_server import IMAGE, SENTINEL, VIDEO


@contextmanager
def running_server(
    tmp_path: Path,
    *,
    backend="h11",
    mode="success",
    known=True,
    kind="image",
    user_resolution=False,
):
    """保留 socket 避免選埠競態，並有界停止獨立 Uvicorn 程序。"""
    with socket.socket() as listener, (tmp_path / "server.log").open("w+") as log:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {
            **os.environ,
            "STREAM_MODE": mode,
            "KNOWN_LENGTH": str(int(known)),
            "MEDIA_KIND": kind,
            "STORY_USER_RESOLUTION": str(int(user_resolution)),
        }
        if user_resolution:
            fake_bin = tmp_path / "fake-bin"
            fake_bin.mkdir()
            executable = fake_bin / "gallery-dl"
            executable.write_text(
                f"#!{sys.executable}\nimport sys\n"
                f"sys.path.insert(0, {str(Path(__file__).parents[2])!r})\n"
                f"from tests.integration.story_gallery import main\nmain({kind!r})\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            cookie_file = tmp_path / "synthetic-cookies.txt"
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            cookie_file.chmod(0o600)
            env["SNS_MEDIA_INSTAGRAM_COOKIE_FILE"] = str(cookie_file)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.integration.download_server:factory",
                "--factory",
                "--fd",
                str(listener.fileno()),
                "--http",
                backend,
                "--no-access-log",
                "--timeout-graceful-shutdown",
                "1",
            ],
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
            env=env,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    if httpx.get(origin + "/healthz", timeout=0.2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "server readiness timeout"
                time.sleep(0.02)
            yield origin
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def extract(origin: str, *, target_path: str = "/p/fixture/") -> dict[str, Any]:
    """透過正式 extraction API 取得指定貼文、Reel 或 Story 的 purpose-bound token。"""
    response = httpx.post(
        origin + "/api/extractions", json={"url": "https://www.instagram.com" + target_path}
    )
    assert response.status_code == 200, response.text
    return response.json()["media"][0]


@pytest.mark.parametrize("mode", ["success", "error"])
def test_story_list_distinct_downloads_and_cleanup(tmp_path: Path, mode: str) -> None:
    """清單各 token 經真實 HTTP 取得對應完整檔案，失敗後釋放 upstream。"""
    with running_server(tmp_path, mode=mode) as origin:
        response = httpx.post(
            origin + "/api/extractions",
            json={"url": "https://www.instagram.com/stories/example.user/"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["post_url"] == "https://www.instagram.com/stories/example.user/"
        assert [item["filename"] for item in payload["media"]] == [
            "instagram-101-1.jpg",
            "instagram-202-1.mp4",
        ]
        for item, expected in zip(payload["media"], [IMAGE, VIDEO], strict=True):
            assert httpx.head(origin + item["download_url"]).status_code == 204
            if mode == "success":
                download = httpx.get(origin + item["download_url"])
                assert download.content == expected
                assert item["filename"] in download.headers["content-disposition"]
            else:
                with pytest.raises(httpx.RemoteProtocolError):
                    httpx.get(origin + item["download_url"])
        stats = httpx.get(origin + "/fixture-stats").json()
        assert stats["fetches"] == stats["closes"] == 2
        assert stats["active"] == 0
    lifecycle = [
        event for event in events(tmp_path) if event["event"].startswith("media_download_")
    ]
    terminal = [event for event in lifecycle if event["event"] != "media_download_started"]
    assert len(terminal) == 2
    assert {event["event"] for event in terminal} == {
        "media_download_completed" if mode == "success" else "media_download_failed"
    }
    assert SENTINEL not in (tmp_path / "server.log").read_text()


def events(tmp_path: Path) -> list[dict[str, Any]]:
    """只解析實際程序輸出的 application JSON 行。"""
    return [
        json.loads(line)
        for line in (tmp_path / "server.log").read_text().splitlines()
        if line.startswith("{")
    ]


@pytest.mark.parametrize("backend", ["h11", "httptools"])
@pytest.mark.parametrize("known", [True, False], ids=["length", "chunked"])
@pytest.mark.parametrize("mode", ["success", "error", "timeout"])
def test_real_http_completion(tmp_path: Path, backend: str, known: bool, mode: str) -> None:
    """核對完整媒體或可識別中止，完整 stderr 不得洩漏原始例外。"""
    with running_server(tmp_path, backend=backend, mode=mode, known=known) as origin:
        media = extract(origin)
        assert httpx.head(origin + media["download_url"]).status_code == 204
        if mode == "success":
            response = httpx.get(origin + media["download_url"])
            assert response.content == IMAGE
        else:
            with pytest.raises(httpx.RemoteProtocolError):
                httpx.get(origin + media["download_url"])
        assert httpx.get(origin + "/fixture-stats").json() == {
            "fetches": 1,
            "closes": 1,
            "ranges": [None],
            "active": 0,
            "methods": ["HEAD", "GET"],
        }
    stderr = (tmp_path / "server.log").read_text()
    assert SENTINEL not in stderr
    assert "Caught handled exception" not in stderr
    assert "ASGI callable returned without completing response" not in stderr
    assert "StreamAborted" in stderr if mode != "success" else "Traceback" not in stderr
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    assert [event["event"] for event in records] == [
        "media_download_started",
        "media_download_completed" if mode == "success" else "media_download_failed",
    ]
    assert records[1]["bytes_streamed"] == (len(IMAGE) if mode == "success" else 65536)


def test_default_process_emits_lifecycle(tmp_path: Path) -> None:
    """不安裝 caplog 或外部 handler，要求預設設定輸出 INFO 事件。"""
    with running_server(tmp_path, mode="error") as origin:
        media = extract(origin)
        with pytest.raises(httpx.RemoteProtocolError):
            httpx.get(origin + media["download_url"])
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    assert [event["event"] for event in records] == [
        "media_download_started",
        "media_download_failed",
    ]
    assert records[1]["bytes_streamed"] == 65536
    assert records[1]["reason_code"] == "unexpected_upstream_failure"
    assert records[1]["platform"] == "instagram"
    assert records[1]["media_class"] == "image"
    assert records[1]["duration_ms"] >= 0
    assert records[1]["request_id"] == records[0]["request_id"]


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("mode", ["success", "error"])
def test_pinned_story_user_resolution_downloads_exact_media_over_real_http(
    tmp_path: Path,
    kind: str,
    mode: str,
) -> None:
    """串接實際套件帳號解析、DataJob、正式 runner 與 HTTP 完整／中斷下載。"""
    with (
        running_server(tmp_path, kind=kind, mode=mode, user_resolution=True) as origin,
        httpx.Client() as client,
    ):
        response = httpx.post(
            origin + "/api/extractions",
            json={
                "url": "https://www.instagram.com/stories/example.user/1234567890/",
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result["media"]) == 1
        assert result["post_url"] == "https://www.instagram.com/stories/example.user/1234567890/"
        media = result["media"][0]
        extension = "jpg" if kind == "image" else "mp4"
        assert media["filename"] == f"instagram-1234567890-1.{extension}"
        assert media["download_url"].startswith("/api/media/")
        assert "scontent.cdninstagram.com" not in response.text
        if mode == "success":
            download = client.get(origin + media["download_url"])
            assert download.content == (IMAGE if kind == "image" else VIDEO)
        else:
            with pytest.raises(httpx.RemoteProtocolError):
                client.get(origin + media["download_url"])
        stats = client.get(origin + "/fixture-stats").json()
        assert stats["fetches"] == stats["closes"] == 1
        assert stats["active"] == 0
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    assert [event["event"] for event in records] == [
        "media_download_started",
        "media_download_completed" if mode == "success" else "media_download_failed",
    ]


@pytest.mark.parametrize("backend", ["h11", "httptools"])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("mode", ["success", "error"])
def test_wire_has_no_fake_completion(tmp_path: Path, backend, known, mode) -> None:
    """直接核對 wire framing，不允許第二份 response 或偽造 chunk terminator。"""
    with running_server(tmp_path, backend=backend, mode=mode, known=known) as origin:
        media = extract(origin)
        address = ("127.0.0.1", int(origin.rsplit(":", 1)[1]))
        with socket.create_connection(address, timeout=3) as sock:
            sock.sendall(
                (
                    f"GET {media['download_url']} HTTP/1.1\r\n"
                    "Host: localhost\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        wire = b"".join(chunks)
    assert wire.count(b"HTTP/1.1") == 1
    headers, body = wire.split(b"\r\n\r\n", 1)
    assert headers.startswith(b"HTTP/1.1 200")
    if known:
        assert f"content-length: {len(IMAGE)}".encode() in headers.lower()
        assert body == (IMAGE if mode == "success" else IMAGE[:65536])
    else:
        assert b"transfer-encoding: chunked" in headers.lower()
        assert body.endswith(b"\r\n0\r\n\r\n") == (mode == "success")
        assert IMAGE[:65536] in body


@pytest.mark.parametrize("mode", ["truncated", "sendfailure", "reject", "postcomplete"])
def test_real_failure_boundaries(tmp_path: Path, mode) -> None:
    """驗證 pre-start、send failure、truncation 與完成後 cleanup 的實際結果。"""
    with running_server(tmp_path, mode=mode) as origin:
        media = extract(origin)
        if mode in {"truncated", "sendfailure"}:
            with pytest.raises(httpx.RemoteProtocolError):
                httpx.get(origin + media["download_url"])
        else:
            response = httpx.get(origin + media["download_url"])
            if mode == "reject":
                assert response.status_code == 502
                assert response.json()["code"] == "upstream_media_invalid"
            else:
                assert response.content == IMAGE
        assert httpx.get(origin + "/fixture-stats").json() == {
            "fetches": 1,
            "closes": 1,
            "ranges": [None],
            "active": 0,
            "methods": ["GET"],
        }
    stderr = (tmp_path / "server.log").read_text()
    assert SENTINEL not in stderr
    assert "ASGI callable returned without completing response" not in stderr
    assert "Caught handled exception" not in stderr
    if mode in {"reject", "postcomplete"}:
        assert "Traceback" not in stderr
    terminal = [event for event in events(tmp_path) if event["event"] == "media_download_failed"]
    assert len(terminal) == 1
    assert not any(event["event"] == "media_download_completed" for event in events(tmp_path))
    assert terminal[0]["bytes_streamed"] == (
        len(IMAGE) if mode == "postcomplete" else 0 if mode == "reject" else 65536
    )


@pytest.mark.parametrize("backend", ["h11", "httptools"])
@pytest.mark.parametrize("mode", ["resume", "truncated", "resume-ignored", "resume-invalid-range"])
def test_instagram_video_range_resume(tmp_path: Path, backend: str, mode: str) -> None:
    """真實 HTTP 驗證 Instagram 續傳的 bytes、framing、terminal event 與清理。"""
    # 成功案例保留連線直到伺服器完成回應；刻意中斷另由 disconnect 案例驗證。
    with (
        running_server(tmp_path, backend=backend, mode=mode, kind="video") as origin,
        httpx.Client() as client,
    ):
        media = extract(origin)
        if mode == "resume":
            response = client.get(origin + media["download_url"])
            assert response.content == VIDEO
            assert response.headers["content-length"] == str(len(VIDEO))
        else:
            with pytest.raises(httpx.RemoteProtocolError):
                client.get(origin + media["download_url"])
        assert client.get(origin + "/fixture-stats").json() == {
            "fetches": 2,
            "closes": 2,
            "ranges": [None, 65536],
            "active": 0,
            "methods": ["GET"],
        }
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    success = mode == "resume"
    assert [event["event"] for event in records] == [
        "media_download_started",
        "media_download_resume_attempted",
        "media_download_resume_succeeded" if success else "media_download_resume_failed",
        "media_download_completed" if success else "media_download_failed",
    ]
    assert len({event["request_id"] for event in records}) == 1
    assert all(event["platform"] == "instagram" for event in records)
    assert records[1]["resume_attempt"] == records[2]["resume_attempt"] == 1
    assert records[-1]["bytes_streamed"] == (
        len(VIDEO) if success else 65536 + 4096 if mode == "truncated" else 65536
    )
    if not success:
        assert records[-1]["reason_code"] == (
            "upstream_truncation" if mode == "truncated" else "upstream_validation"
        )
    stderr = (tmp_path / "server.log").read_text()
    assert SENTINEL not in stderr
    assert ("StreamAborted" in stderr) == (not success)


@pytest.mark.parametrize("user_resolution", [False, True])
def test_real_disconnect_cleanup(tmp_path: Path, user_resolution: bool) -> None:
    """client 在首段後關閉 socket，server 有界釋放 lease 並只記錄 aborted。"""
    with running_server(tmp_path, mode="disconnect", user_resolution=user_resolution) as origin:
        media = extract(
            origin,
            target_path="/stories/example.user/1234567890/" if user_resolution else "/p/fixture/",
        )
        with httpx.stream("GET", origin + media["download_url"]) as response:
            assert next(response.iter_bytes())
        deadline = time.monotonic() + 3
        while True:
            stats = httpx.get(origin + "/fixture-stats").json()
            if stats["active"] == 0:
                break
            assert time.monotonic() < deadline, stats
            time.sleep(0.02)
        assert stats == {
            "fetches": 1,
            "closes": 1,
            "ranges": [None],
            "active": 0,
            "methods": ["GET"],
        }
    records = [event for event in events(tmp_path) if event["event"].startswith("media_download")]
    assert [event["event"] for event in records] == [
        "media_download_started",
        "media_download_aborted",
    ]
    assert records[1]["reason_code"] == "client_disconnect"
