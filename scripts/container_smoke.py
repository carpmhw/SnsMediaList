"""Run startup and isolation checks against the Docker Compose service."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"


def run_command(
    command: list[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one Docker command without invoking a shell."""
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        capture_output=True,
        env=env,
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
    """Verify the runtime exposes the FFmpeg version pinned by the Dockerfile."""
    result = run_command(["docker", "exec", container_id, "ffmpeg", "-version"])
    if not result.stdout.startswith("ffmpeg version 5.1.9-"):
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


def main() -> int:
    """Build, start, inspect, restart, and gracefully stop the service."""
    if shutil.which("docker") is None:
        print("docker is required for container smoke tests", file=sys.stderr)
        return 2

    project = f"sns-media-list-smoke-{os.getpid()}"
    compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    environment = os.environ.copy()
    environment["SNS_MEDIA_HOST_PORT"] = str(find_free_port())
    container_id = ""
    try:
        run_command([*compose, "config", "--quiet"], env=environment)
        run_command([*compose, "build", "--pull=false"], env=environment)
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
        print("container smoke checks passed")
        return 0
    finally:
        run_command([*compose, "down", "--remove-orphans"], check=False, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
