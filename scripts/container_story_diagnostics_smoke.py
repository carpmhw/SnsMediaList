"""以唯讀 fake extractor 與假 Cookie 驗證 candidate container 的 Story 日誌。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"
CANDIDATE_IMAGE = "sns-media-list:story-diagnostics-candidate"
STORY_URL = "https://www.instagram.com/stories/example.user/1234567890/"
COOKIE_CONTAINER_PATH = "/run/secrets/instagram-cookies.txt"
FAKE_GALLERY_PATH = "/opt/venv/bin/gallery-dl"
_STORY_MEDIA_IDS = ("1234567890", "1234567891", "1234567892", "1234567893")
_SCENARIOS = (
    (
        "unknown-stderr",
        "https://www.instagram.com/stories/example.user/1234567891/",
        None,
        502,
        "extraction_failed",
        "extractor_process_unclassified",
    ),
    (
        "unknown-datajob",
        "https://www.instagram.com/stories/example.user/1234567892/",
        None,
        502,
        "extraction_failed",
        "extractor_process_unclassified",
    ),
    (
        "invalid-json",
        "https://www.instagram.com/stories/example.user/1234567893/",
        None,
        502,
        "extraction_failed",
        "extractor_invalid_output",
    ),
    (
        "anonymous-auth-required",
        STORY_URL,
        None,
        403,
        "story_auth_required",
        "extractor_platform_error",
    ),
    (
        "configured-auth-required",
        STORY_URL,
        COOKIE_CONTAINER_PATH,
        503,
        "platform_authentication_failed",
        "extractor_platform_error",
    ),
)
_SENTINELS = (
    *(
        f"https://www.instagram.com/stories/example.user/{media_id}/"
        for media_id in _STORY_MEDIA_IDS
    ),
    "example.user",
    *_STORY_MEDIA_IDS,
    "FAKE_CONTAINER_QUERY_SENTINEL",
    "FAKE_CONTAINER_COOKIE_SENTINEL",
    "FAKE_CONTAINER_TOKEN_SENTINEL",
    "PRIVATE_CONTAINER_RAW_DIAGNOSTIC",
    "PRIVATE_CONTAINER_INVALID_JSON",
)


def _run_command(
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    timeout: float = 600,
) -> str:
    """執行不經 shell 的 bounded Docker command，成功時回傳 stdout。"""
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError("Docker Story diagnostics smoke command failed.")
    return result.stdout


def _find_free_port() -> int:
    """取得可供單一 smoke container 綁定的 loopback host port。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fake_gallery_dl(directory: Path) -> Path:
    """建立唯讀 fake gallery-dl，依 Story ID 輸出多種合成 failure。"""
    executable = directory / "fake-gallery-dl"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys
media_id = sys.argv[-1].rstrip("/").rsplit("/", 1)[-1]
if media_id == "1234567891":
    sys.stderr.write("unrecognized diagnostic PRIVATE_CONTAINER_RAW_DIAGNOSTIC\\n")
    sys.exit(1)
if media_id == "1234567892":
    sys.stdout.write(json.dumps([[-1, {
        "error": "UnknownError",
        "message": "unrecognized DataJob error PRIVATE_CONTAINER_RAW_DIAGNOSTIC",
    }]]))
    sys.exit(0)
if media_id == "1234567893":
    sys.stdout.write("PRIVATE_CONTAINER_INVALID_JSON")
    sys.exit(0)
sys.stderr.write(
    "AuthRequired: authenticated cookies needed; "
    + sys.argv[-1] + "?q=FAKE_CONTAINER_QUERY_SENTINEL "
    "sessionid=FAKE_CONTAINER_COOKIE_SENTINEL token=FAKE_CONTAINER_TOKEN_SENTINEL\\n"
)
sys.exit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _write_fake_cookie(directory: Path) -> Path:
    """建立僅供測試掛載的 Netscape Cookie fixture，不接觸真實平台。"""
    cookie_file = directory / "fake-instagram-cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t"
        "FAKE_CONTAINER_COOKIE_SENTINEL\n",
        encoding="utf-8",
    )
    cookie_file.chmod(0o444)
    return cookie_file


def _compose_args(project: str, override_file: Path) -> list[str]:
    """建立使用獨立 project 與臨時唯讀測試 override 的 Compose 命令。"""
    return [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(override_file),
    ]


def _write_compose_override(
    path: Path,
    *,
    fake_gallery: Path,
    cookie_file: Path | None,
    proxy_port: int,
) -> None:
    """寫入只增加 candidate image 與唯讀測試掛載的臨時 Compose override。"""
    lines = [
        "services:",
        "  app:",
        f"    image: {json.dumps(CANDIDATE_IMAGE)}",
        "    environment:",
        f'      SNS_MEDIA_EXTRACTION_PROXY_PORT: "{proxy_port}"',
    ]
    if cookie_file is not None:
        lines.extend(
            [
                f"      SNS_MEDIA_INSTAGRAM_COOKIE_FILE: {json.dumps(COOKIE_CONTAINER_PATH)}",
            ]
        )
    lines.extend(
        [
            "    volumes:",
            "      - type: bind",
            f"        source: {json.dumps(str(fake_gallery))}",
            f"        target: {FAKE_GALLERY_PATH}",
            "        read_only: true",
        ]
    )
    if cookie_file is not None:
        lines.extend(
            [
                "      - type: bind",
                f"        source: {json.dumps(str(cookie_file))}",
                f"        target: {COOKIE_CONTAINER_PATH}",
                "        read_only: true",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wait_for_health(origin: str, timeout: float = 90) -> None:
    """以 bounded retry 等候 container health endpoint ready。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{origin}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError("candidate Story diagnostics container did not become healthy")


def _post_story_extraction(origin: str, story_url: str) -> tuple[int, dict[str, Any]]:
    """透過正式 HTTP API 執行一次精確 Story extraction。"""
    request = urllib.request.Request(
        f"{origin}/api/extractions",
        data=json.dumps({"url": story_url}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
    with response:
        return response.status, json.loads(response.read())


def _verify_runtime_limits(
    compose: list[str],
    env: dict[str, str],
    *,
    cookie_configured: bool,
) -> str:
    """確認 candidate container 保留非 root、read-only root 與唯讀測試掛載。"""
    container_id = _run_command([*compose, "ps", "-q", "app"], env=env).strip()
    if not container_id:
        raise RuntimeError("candidate Compose project did not create its app container")
    user = _run_command(["docker", "inspect", "-f", "{{.Config.User}}", container_id], env=env)
    if user.strip() != "10001:10001":
        raise RuntimeError("candidate container is not running as UID/GID 10001")
    host_config = json.loads(
        _run_command(["docker", "inspect", "-f", "{{json .HostConfig}}", container_id], env=env)
    )
    if not host_config.get("ReadonlyRootfs"):
        raise RuntimeError("candidate container root filesystem is writable")
    command = json.loads(
        _run_command(["docker", "inspect", "-f", "{{json .Config.Cmd}}", container_id], env=env)
    )
    if (
        command.count("--workers") != 1
        or command[command.index("--workers") + 1] != "1"
        or "--no-access-log" not in command
    ):
        raise RuntimeError("candidate container lost its worker or access-log boundary")
    ports = json.loads(
        _run_command(
            ["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container_id],
            env=env,
        )
    )
    bindings = ports.get("8000/tcp") or []
    if not bindings or any(
        binding.get("HostIp") not in {"127.0.0.1", "::1"} for binding in bindings
    ):
        raise RuntimeError("candidate application port is not loopback-only")
    mounts = json.loads(
        _run_command(["docker", "inspect", "-f", "{{json .Mounts}}", container_id], env=env)
    )
    destinations = [FAKE_GALLERY_PATH]
    if cookie_configured:
        destinations.append(COOKIE_CONTAINER_PATH)
    for destination in destinations:
        mounted = [mount for mount in mounts if mount.get("Destination") == destination]
        if destination == COOKIE_CONTAINER_PATH and not any(
            mount.get("RW") is False for mount in mounted
        ):
            raise RuntimeError("candidate Cookie fixture is not mounted read-only")
        if destination == FAKE_GALLERY_PATH and not any(
            mount.get("RW") is False for mount in mounted
        ):
            raise RuntimeError("candidate fake extractor is not mounted read-only")
    return container_id


def _verify_mode(
    temporary_directory: Path,
    *,
    fake_gallery: Path,
    cookie_file: Path | None,
    scenario_name: str,
    story_url: str,
    expected_status: int,
    expected_reason: str,
    expected_stage: str,
    build_candidate: bool,
) -> None:
    """啟動單一隔離 Compose project 並驗證 HTTP response 與唯一安全 event。"""
    mode = "configured" if cookie_file is not None else "anonymous"
    project = f"sns-story-diagnostics-{uuid.uuid4().hex[:10]}-{scenario_name}-{mode}"
    host_port = _find_free_port()
    proxy_port = _find_free_port()
    environment = os.environ.copy()
    environment["SNS_MEDIA_HOST_PORT"] = str(host_port)
    override_file = temporary_directory / f"{scenario_name}-{mode}-compose.override.yaml"
    _write_compose_override(
        override_file,
        fake_gallery=fake_gallery,
        cookie_file=cookie_file,
        proxy_port=proxy_port,
    )
    compose = _compose_args(project, override_file)
    try:
        _run_command([*compose, "config", "--quiet"], env=environment)
        compose_config = _run_command([*compose, "config"], env=environment)
        if "--no-access-log" not in compose_config or "--workers" not in compose_config:
            raise RuntimeError("candidate Compose command lost its access-log or worker boundary")
        if build_candidate:
            _run_command([*compose, "build", "--pull=false", "app"], env=environment)
            identity = _run_command(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}} {{json .RepoDigests}}",
                    CANDIDATE_IMAGE,
                ],
                env=environment,
            ).strip()
            print(f"candidate image ID and repository digests: {identity}")
        _run_command([*compose, "up", "-d", "--no-build", "app"], env=environment)
        _verify_runtime_limits(
            compose,
            environment,
            cookie_configured=cookie_file is not None,
        )
        origin = f"http://127.0.0.1:{host_port}"
        _wait_for_health(origin)
        status, payload = _post_story_extraction(origin, story_url)
        logs = _run_command(
            [*compose, "logs", "--no-color", "--no-log-prefix", "app"],
            env=environment,
        )
        if status != expected_status or payload.get("code") != expected_reason:
            raise RuntimeError("candidate container returned an unexpected extraction error")

        events: list[dict[str, Any]] = []
        for line in logs.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("event", "").startswith("extraction_"):
                events.append(value)
        if len(events) != 1 or events[0].get("event") != "extraction_failed":
            raise RuntimeError("candidate container did not emit one extraction terminal event")
        event = events[0]
        if (
            event.get("request_id") != payload.get("request_id")
            or event.get("platform") != "instagram"
            or event.get("outcome") != "failed"
            or event.get("reason_code") != expected_reason
            or event.get("failure_stage") != expected_stage
        ):
            raise RuntimeError("candidate container emitted an inconsistent extraction event")
        if "failure_stage" in json.dumps(payload):
            raise RuntimeError("candidate container exposed failure_stage in the API response")
        if "POST /api/extractions" in logs or any(sentinel in logs for sentinel in _SENTINELS):
            raise RuntimeError("candidate container logs contain access or sensitive test data")
        if any(sentinel in json.dumps(payload) for sentinel in _SENTINELS):
            raise RuntimeError("candidate container response contains sensitive test data")
        print(
            f"{scenario_name}/{mode}: HTTP {status}, reason_code={expected_reason}, "
            f"failure_stage={expected_stage}"
        )
    finally:
        _run_command([*compose, "down", "--remove-orphans"], env=environment)


def main() -> int:
    """建置 candidate image、驗證固定 failure matrix 並清理隔離 Compose projects。"""
    if shutil.which("docker") is None:
        print("docker is required for container Story diagnostics smoke", file=sys.stderr)
        return 2
    docker_info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if docker_info.returncode != 0:
        print("Docker daemon is unavailable for container Story diagnostics smoke", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="sns-story-diagnostics-") as directory_name:
            temporary_directory = Path(directory_name)
            fake_gallery = _write_fake_gallery_dl(temporary_directory)
            cookie_file = _write_fake_cookie(temporary_directory)
            for index, (
                scenario_name,
                story_url,
                configured_cookie_path,
                expected_status,
                expected_reason,
                expected_stage,
            ) in enumerate(_SCENARIOS):
                _verify_mode(
                    temporary_directory,
                    fake_gallery=fake_gallery,
                    cookie_file=cookie_file if configured_cookie_path is not None else None,
                    scenario_name=scenario_name,
                    story_url=story_url,
                    expected_status=expected_status,
                    expected_reason=expected_reason,
                    expected_stage=expected_stage,
                    build_candidate=index == 0,
                )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
        print(f"container Story diagnostics smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
