"""Run startup and isolation checks against the Docker Compose service."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"
MCP_COMPOSE_FILE = PROJECT_ROOT / "docker-compose.mcp.yaml"


def run_command(
    command: list[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """以不經 shell 的方式執行一個有界 Docker 或 smoke 命令。"""
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def find_free_port() -> int:
    """Reserve and return an ephemeral host port for an isolated smoke container."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_health(compose: list[str], container_id: str, timeout: float = 90.0) -> None:
    """Wait until the Compose container reports a healthy status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run_command(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", container_id],
            check=False,
        )
        status = result.stdout.strip()
        if status == "healthy":
            return
        if status == "unhealthy":
            raise RuntimeError("container healthcheck reported unhealthy")
        time.sleep(1)
    raise TimeoutError("container did not become healthy before the deadline")


def exec_python(container_id: str, source: str) -> None:
    """Execute a short assertion script as the image's configured user."""
    result = run_command(["docker", "exec", container_id, "python", "-c", source], check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "container assertion failed")


def verify_ffmpeg(container_id: str) -> None:
    """確認 Alpine runtime FFmpeg 版本符合 Dockerfile 精確 pin。"""
    result = run_command(["docker", "exec", container_id, "ffmpeg", "-version"])
    if not result.stdout.startswith("ffmpeg version 8.1.2 "):
        raise RuntimeError("container FFmpeg version does not match the pinned runtime")


def verify_uvicorn(container_id: str) -> None:
    """Verify relocated virtualenv console scripts keep a valid interpreter path."""
    run_command(["docker", "exec", container_id, "uvicorn", "--version"])


def verify_network_binding(container_id: str) -> None:
    """Verify the application port is published only on the host loopback."""
    result = run_command(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container_id]
    )
    ports = json.loads(result.stdout)
    bindings = ports.get("8000/tcp") or []
    if not bindings or any(
        binding.get("HostIp") not in {"127.0.0.1", "::1"} for binding in bindings
    ):
        raise RuntimeError("container application port is not loopback-only")


def verify_runtime_hardening(container_id: str) -> None:
    """Verify capabilities, privilege escalation, and process-count bounds."""
    result = run_command(["docker", "inspect", "-f", "{{json .HostConfig}}", container_id])
    host_config = json.loads(result.stdout)
    if "ALL" not in (host_config.get("CapDrop") or []):
        raise RuntimeError("container capabilities were not dropped")
    if "no-new-privileges:true" not in (host_config.get("SecurityOpt") or []):
        raise RuntimeError("container no-new-privileges is not enabled")
    if int(host_config.get("PidsLimit") or 0) <= 0:
        raise RuntimeError("container PID limit is not bounded")


def verify_mcp_disabled(container_id: str) -> None:
    """確認 base image 的 MCP GET／POST 均為 404，不會自動增加能力。"""
    exec_python(
        container_id,
        """from urllib.error import HTTPError
from urllib.request import Request, urlopen
for method in ('GET', 'POST'):
    request = Request('http://127.0.0.1:8000/mcp', method=method,
                      data=b'{}' if method == 'POST' else None)
    try:
        urlopen(request, timeout=5).close()
    except HTTPError as error:
        assert error.code == 404
    else:
        raise AssertionError('disabled MCP is reachable')
""",
    )


def verify_mcp_runtime(container_id: str) -> None:
    """依 source pins 比對 container SDK／Pydantic，並輸出非敏感 runtime 版本。"""
    dependencies = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    expected = {
        name: next(
            dependency.split("==", 1)[1]
            for dependency in dependencies
            if dependency.startswith(f"{pin}==")
        )
        for name, pin in (("mcp", "mcp[cli]"), ("pydantic", "pydantic"))
    }
    source = f"""from importlib.metadata import version
expected = {expected!r}
assert all(version(name) == pin for name, pin in expected.items())
print('MCP runtime:', ', '.join(name + '==' + version(name) for name in expected))
"""
    result = run_command(["docker", "exec", container_id, "python", "-c", source])
    print(result.stdout, end="")


def verify_mcp_protocol(environment: Mapping[str, str]) -> None:
    """對隔離 candidate 執行現代／legacy smoke，不傳入 live extraction URL。"""
    endpoint = f"http://127.0.0.1:{environment['SNS_MEDIA_HOST_PORT']}/mcp"
    for mode in ("auto", "legacy"):
        result = run_command(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "mcp_smoke.py"),
                endpoint,
                "--mode",
                mode,
            ],
            env=environment,
            timeout=90,
        )
        print(result.stdout, end="")


def main() -> int:
    """使用獨立 image tag 建置、啟動、檢查、重啟並停止 smoke service。"""
    if shutil.which("docker") is None:
        print("docker is required for container smoke tests", file=sys.stderr)
        return 2

    project = f"sns-media-list-smoke-{os.getpid()}"
    compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    environment = os.environ.copy()
    environment["SNS_MEDIA_HOST_PORT"] = str(find_free_port())
    image_tag = f"smoke-{os.getpid()}"
    image_reference = f"sns-media-list:{image_tag}"
    environment["SNS_MEDIA_IMAGE_TAG"] = image_tag
    container_id = ""
    try:
        run_command([*compose, "config", "--quiet"], env=environment)
        run_command([*compose, "build", "--pull=false"], env=environment)
        image_identity = run_command(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}} {{json .RepoDigests}}",
                image_reference,
            ],
            env=environment,
        ).stdout.strip()
        print(
            f"container smoke image {image_reference} ID and repository digests: {image_identity}"
        )
        run_command([*compose, "up", "-d"], env=environment)
        container_id = run_command([*compose, "ps", "-q", "app"], env=environment).stdout.strip()
        if not container_id:
            raise RuntimeError("Compose did not create the app container")
        wait_for_health(compose, container_id)

        user_id = run_command(["docker", "exec", container_id, "id", "-u"]).stdout.strip()
        if user_id != "10001":
            raise RuntimeError(f"container is running as UID {user_id}, expected 10001")
        verify_network_binding(container_id)
        verify_runtime_hardening(container_id)
        verify_ffmpeg(container_id)
        verify_uvicorn(container_id)
        verify_mcp_runtime(container_id)
        verify_mcp_disabled(container_id)
        exec_python(
            container_id,
            """import errno
from pathlib import Path
try:
    Path('/app/smoke-write').write_text('blocked')
except OSError as error:
    if error.errno != errno.EROFS:
        raise
else:
    raise SystemExit('read-only root filesystem check failed')
Path('/tmp/restart-marker').write_text('ephemeral')
assert not Path('/app/media').exists()
""",
        )

        run_command([*compose, "restart", "app"], env=environment)
        container_id = run_command([*compose, "ps", "-q", "app"], env=environment).stdout.strip()
        wait_for_health(compose, container_id)
        exec_python(
            container_id,
            "from pathlib import Path; assert not Path('/tmp/restart-marker').exists()",
        )

        run_command([*compose, "stop", "-t", "10"], env=environment)
        state = run_command(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_id]
        ).stdout.strip()
        if state != "exited":
            raise RuntimeError(f"graceful shutdown left container in state {state}")

        mcp_compose = [*compose, "-f", str(MCP_COMPOSE_FILE)]
        run_command([*mcp_compose, "config", "--quiet"], env=environment)
        run_command([*mcp_compose, "up", "-d", "--no-build"], env=environment)
        container_id = run_command(
            [*mcp_compose, "ps", "-q", "app"], env=environment
        ).stdout.strip()
        wait_for_health(mcp_compose, container_id)
        verify_network_binding(container_id)
        verify_runtime_hardening(container_id)
        verify_mcp_runtime(container_id)
        verify_mcp_protocol(environment)
        run_command([*mcp_compose, "restart", "app"], env=environment)
        wait_for_health(mcp_compose, container_id)
        verify_mcp_protocol(environment)
        run_command([*mcp_compose, "stop", "-t", "10"], env=environment)
        state = run_command(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_id]
        ).stdout.strip()
        if state != "exited":
            raise RuntimeError("MCP graceful shutdown left container running")
        diagnostic_environment = environment.copy()
        diagnostic_environment["SNS_MEDIA_STORY_DIAGNOSTICS_IMAGE"] = image_reference
        diagnostics_result = run_command(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "container_story_diagnostics_smoke.py"),
            ],
            env=diagnostic_environment,
            timeout=1200,
        )
        print(diagnostics_result.stdout, end="")
        print("container smoke checks passed")
        return 0
    finally:
        run_command([*compose, "down", "--remove-orphans"], check=False, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
