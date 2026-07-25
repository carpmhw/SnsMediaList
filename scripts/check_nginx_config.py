"""Run a repeatable syntax check for the repository Nginx example."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

_NGINX_IMAGE = "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"


def _run(command: Sequence[str]) -> int:
    """Run one syntax-check command and print diagnostics only on failure."""
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return 0
    if result.stderr:
        print(result.stderr, end="")
    return result.returncode or 1


def _local_command(config: Path, directory: Path) -> list[str]:
    """Create a minimal Nginx root configuration around the server example."""
    root = directory / "nginx.conf"
    root.write_text(
        f'events {{}}\nhttp {{\n    include "{config.resolve()}";\n}}\n',
        encoding="utf-8",
    )
    return [
        "nginx",
        "-t",
        "-c",
        str(root),
        "-g",
        f"pid {directory / 'nginx.pid'};",
    ]


def _docker_command(config: Path) -> list[str]:
    """Build a syntax-check command using the immutable Nginx image fallback."""
    mount = f"{config.resolve()}:/etc/nginx/conf.d/default.conf:ro"
    return ["docker", "run", "--rm", "-v", mount, _NGINX_IMAGE, "nginx", "-t"]


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate the Nginx example with a local binary or pinned container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("deploy/nginx/sns-media-list.conf"),
    )
    options = parser.parse_args(arguments)
    if not options.config.is_file():
        parser.error(f"Nginx configuration does not exist: {options.config}")
    if shutil.which("nginx"):
        with tempfile.TemporaryDirectory(prefix="sns-media-list-nginx-") as directory:
            command = _local_command(options.config, Path(directory))
            status = _run(command)
    elif shutil.which("docker"):
        status = _run(_docker_command(options.config))
    else:
        print("neither nginx nor docker is available")
        return 2
    if status == 0:
        print("nginx syntax check passed")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
