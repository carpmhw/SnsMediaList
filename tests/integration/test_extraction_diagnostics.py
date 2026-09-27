"""透過獨立 Uvicorn 程序驗證擷取失敗的預設結構化日誌。"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).parents[2]
STORY_URL = "https://www.instagram.com/stories/example.user/1234567890/"
_STORY_MEDIA_IDS = ("1234567890", "1234567891", "1234567892", "1234567893", "1234567894")
SENSITIVE_SENTINELS = (
    *(
        f"https://www.instagram.com/stories/example.user/{media_id}/"
        for media_id in _STORY_MEDIA_IDS
    ),
    "example.user",
    *_STORY_MEDIA_IDS,
    "FAKE_QUERY_SENTINEL",
    "FAKE_SESSION_SENTINEL",
    "FAKE_TOKEN_SENTINEL",
    "PRIVATE_RAW_DIAGNOSTIC",
    "PRIVATE_INVALID_JSON",
)


def _find_available_port() -> int:
    """取得一個可供測試程序使用的 loopback port。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _install_fake_gallery_dl(directory: Path) -> Path:
    """建立依 Story ID 輸出多種合成 extractor failure 的 fake gallery-dl。"""
    directory.mkdir()
    executable = directory / "gallery-dl"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys
media_id = sys.argv[-1].rstrip("/").rsplit("/", 1)[-1]
if media_id == "1234567891":
    sys.stderr.write("unrecognized diagnostic PRIVATE_RAW_DIAGNOSTIC\\n")
    sys.exit(1)
if media_id == "1234567892":
    sys.stdout.write(json.dumps([[-1, {
        "error": "UnknownError",
        "message": "unrecognized DataJob error PRIVATE_RAW_DIAGNOSTIC",
    }]]))
    sys.exit(0)
if media_id == "1234567893":
    sys.stdout.write("PRIVATE_INVALID_JSON")
    sys.exit(0)
if media_id == "1234567894":
    sys.stdout.write(json.dumps([[-1, {
        "error": "HttpError",
        "message": "HTTP 403 Forbidden PRIVATE_RAW_DIAGNOSTIC",
    }]]))
    sys.exit(0)
sys.stderr.write(
    "AuthRequired: authenticated cookies needed; "
    + sys.argv[-1] + "?q=FAKE_QUERY_SENTINEL "
    "sessionid=FAKE_SESSION_SENTINEL token=FAKE_TOKEN_SENTINEL\\n"
)
sys.exit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _start_uvicorn(
    temporary_directory: Path,
    *,
    configured_cookie: Path | None,
) -> tuple[subprocess.Popen[str], str]:
    """以預設 application logging、單 worker 和停用 access log 啟動 Uvicorn。"""
    fake_bin = temporary_directory / "fake-bin"
    _install_fake_gallery_dl(fake_bin)
    port = _find_available_port()
    environment = os.environ.copy()
    environment.pop("SNS_MEDIA_INSTAGRAM_COOKIE_FILE", None)
    environment.pop("SNS_MEDIA_X_COOKIE_FILE", None)
    environment["SNS_MEDIA_EXTRACTION_PROXY_PORT"] = str(_find_available_port())
    environment["PATH"] = os.pathsep.join((str(fake_bin), environment.get("PATH", "")))
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", ""))
    )
    if configured_cookie is not None:
        environment["SNS_MEDIA_INSTAGRAM_COOKIE_FILE"] = str(configured_cookie)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sns_media_list.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while True:
        if process.poll() is not None:
            output, _stderr = process.communicate()
            raise AssertionError(f"isolated Uvicorn exited before readiness: {output}")
        try:
            if httpx.get(f"{origin}/healthz", timeout=0.2).status_code == 200:
                return process, origin
        except httpx.TransportError:
            pass
        if time.monotonic() >= deadline:
            process.terminate()
            output, _stderr = process.communicate(timeout=5)
            raise TimeoutError(f"isolated Uvicorn readiness timeout: {output}")
        time.sleep(0.02)


def _stop_uvicorn(process: subprocess.Popen[str]) -> str:
    """有界停止 Uvicorn，並回傳實際 stdout/stderr 合併內容。"""
    if process.poll() is None:
        process.terminate()
    try:
        output, _stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _stderr = process.communicate(timeout=5)
    return output or ""


@pytest.mark.parametrize(
    (
        "media_id",
        "configured_cookie",
        "expected_status",
        "expected_reason",
        "expected_stage",
        "expected_diagnostics",
    ),
    [
        pytest.param(
            "1234567890",
            False,
            403,
            "story_auth_required",
            "extractor_platform_error",
            {
                "extractor_diagnostic_source": "stderr",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 1,
            },
            id="anonymous-auth-required",
        ),
        pytest.param(
            "1234567890",
            True,
            503,
            "platform_authentication_failed",
            "extractor_platform_error",
            {
                "extractor_diagnostic_source": "stderr",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 1,
            },
            id="configured-auth-required",
        ),
        pytest.param(
            "1234567891",
            False,
            502,
            "extraction_failed",
            "extractor_process_unclassified",
            {
                "extractor_diagnostic_source": "stderr",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 1,
            },
            id="unknown-nonzero-stderr",
        ),
        pytest.param(
            "1234567892",
            False,
            502,
            "extraction_failed",
            "extractor_process_unclassified",
            {
                "extractor_diagnostic_source": "datajob_error",
                "extractor_error_type": "unknown",
                "extractor_exit_code": 0,
            },
            id="unknown-datajob-error",
        ),
        pytest.param(
            "1234567893",
            False,
            502,
            "extraction_failed",
            "extractor_invalid_output",
            {"extractor_exit_code": 0},
            id="invalid-json",
        ),
        pytest.param(
            "1234567894",
            True,
            502,
            "extraction_failed",
            "extractor_process_unclassified",
            {
                "extractor_diagnostic_source": "datajob_error",
                "extractor_error_type": "http_error",
                "extractor_exit_code": 0,
                "extractor_http_statuses": [403],
            },
            id="configured-story-http-403",
        ),
    ],
)
def test_default_uvicorn_process_logs_one_bounded_story_failure(
    tmp_path: Path,
    media_id: str,
    configured_cookie: bool,
    expected_status: int,
    expected_reason: str,
    expected_stage: str,
    expected_diagnostics: dict[str, object],
) -> None:
    """驗證預設 Uvicorn 將不同 runner failure 映射成唯一安全 JSON event。"""
    cookie_file: Path | None = None
    if configured_cookie:
        cookie_file = tmp_path / "fake-instagram-cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        cookie_file.chmod(0o600)
    process, origin = _start_uvicorn(tmp_path, configured_cookie=cookie_file)
    story_url = f"https://www.instagram.com/stories/example.user/{media_id}/"
    try:
        response = httpx.post(
            f"{origin}/api/extractions",
            json={"url": story_url},
            timeout=5,
        )
    finally:
        output = _stop_uvicorn(process)

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_reason
    assert response.headers["X-SNS-Error-Code"] == expected_reason
    for field_name in (
        "failure_stage",
        "extractor_diagnostics",
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    ):
        assert field_name not in response.text
    event_lines = [line for line in output.splitlines() if line.startswith("{")]
    events = [json.loads(line) for line in event_lines]
    assert [event["event"] for event in events] == ["extraction_failed"]
    assert events[0]["request_id"] == response.json()["request_id"]
    assert events[0]["platform"] == "instagram"
    assert events[0]["outcome"] == "failed"
    assert events[0]["reason_code"] == expected_reason
    assert events[0]["failure_stage"] == expected_stage
    for field_name, expected_value in expected_diagnostics.items():
        assert events[0][field_name] == expected_value
    for field_name in {
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    } - expected_diagnostics.keys():
        assert field_name not in events[0]
    assert events[0]["duration_ms"] >= 0
    for sentinel in SENSITIVE_SENTINELS:
        assert sentinel not in output
    assert "GET /healthz" not in output
