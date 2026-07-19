"""Run the gallery-dl adapter contract checks after dependency updates."""

import subprocess
import sys


def main() -> int:
    """Run fixture, quality, process, and egress tests with uv."""
    command = [
        "uv",
        "run",
        "pytest",
        "tests/unit/test_gallery_dl_normalizer.py",
        "tests/unit/test_gallery_dl_quality.py",
        "tests/unit/test_gallery_dl_process.py",
        "tests/unit/test_egress_policy.py",
        "-q",
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
