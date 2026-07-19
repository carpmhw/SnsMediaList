"""Contract tests for the hardened container deployment."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_dockerfile_uses_pinned_runtime_and_non_root_entrypoint() -> None:
    """Verify the image pins Python, installs the locked app, and runs as app."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "python:3.12.3-slim-bookworm" in dockerfile
    assert "gallery-dl==1.32.7" not in dockerfile
    assert "yt-dlp" not in dockerfile
    assert "USER app" in dockerfile
    assert '"sns_media_list.app:create_app", "--factory"' in dockerfile
    assert '"--workers", "1"' in dockerfile


def test_gallery_license_notice_is_shipped() -> None:
    """Verify the pinned extractor license notice is part of the image context."""
    notice = (PROJECT_ROOT / "LICENSES" / "gallery-dl.txt").read_text()

    assert "gallery-dl 1.32.7" in notice
    assert "GPL-2.0-only" in notice
    assert "codeberg.org/mikf/gallery-dl" in notice


def test_compose_enforces_single_non_root_read_only_service() -> None:
    """Verify Compose exposes one worker with bounded ephemeral storage."""
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text()

    assert compose.count("\n  app:") == 1
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert "tmpfs:" in compose
    assert "/tmp:size=64m" in compose
    assert "healthcheck:" in compose
    assert '- --workers\n      - "1"' in compose
    assert "${SNS_MEDIA_HOST_PORT:-8000}:8000" in compose
    assert "volumes:" not in compose
    assert "/media" not in compose


def test_compose_documents_bounded_runtime_settings() -> None:
    """Verify Compose carries the documented network and resource limits."""
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text()

    for setting in (
        "SNS_MEDIA_TOKEN_TTL_SECONDS",
        "SNS_MEDIA_TOKEN_CAPACITY",
        "SNS_MEDIA_EXTRACTION_TIMEOUT_SECONDS",
        "SNS_MEDIA_MAX_DOWNLOAD_BYTES",
        "SNS_MEDIA_MAX_REDIRECTS",
        "SNS_MEDIA_MAX_EXTRACTIONS",
        "SNS_MEDIA_MAX_DOWNLOADS",
    ):
        assert setting in compose


def test_container_smoke_script_checks_runtime_boundaries() -> None:
    """Verify the automated smoke command covers startup and isolation checks."""
    smoke_script = (PROJECT_ROOT / "scripts" / "container_smoke.py").read_text()

    for check in (
        '"config", "--quiet"',
        '"up", "-d"',
        "State.Health.Status",
        "expected 10001",
        "read-only root filesystem check failed",
        "restart-marker",
        '"stop", "-t", "10"',
    ):
        assert check in smoke_script
