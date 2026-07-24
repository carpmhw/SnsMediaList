"""Contract tests for the hardened container deployment."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_dockerfile_uses_pinned_runtime_and_non_root_entrypoint() -> None:
    """Verify the image pins Python, installs the locked app, and runs as app."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "ghcr.io/astral-sh/uv:" in dockerfile
    assert "COPY --from=uv" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile
    assert "/build/.venv" not in dockerfile
    assert "pip install" not in dockerfile
    assert "gallery-dl==1.32.7" not in dockerfile
    assert "yt-dlp" not in dockerfile
    assert "USER app" in dockerfile
    assert '"sns_media_list.app:create_app", "--factory"' in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert "FFMPEG_VERSION" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "COPY LICENSES" in dockerfile


def test_dockerignore_excludes_cookie_material_from_build_context() -> None:
    """Verify Docker builds cannot send local credential files to a builder."""
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text()

    assert "secrets/" in dockerignore
    assert "*.cookies.txt" in dockerignore
    assert "cookies.txt" in dockerignore
    assert "x-cookies.txt" in dockerignore
    assert "gallery-dl.conf" in dockerignore


def test_ffmpeg_license_notice_is_shipped() -> None:
    """Verify the controlled FFmpeg runtime license notice is present."""
    notice = (PROJECT_ROOT / "LICENSES" / "ffmpeg.txt").read_text()

    assert "FFmpeg" in notice
    assert "GPL-2.0" in notice
    assert "ffmpeg.org" in notice


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
    assert "127.0.0.1:${SNS_MEDIA_HOST_PORT:-8000}:8000" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "pids_limit:" in compose
    assert "volumes:" not in compose
    assert "/media" not in compose


def test_compose_documents_bounded_runtime_settings() -> None:
    """Verify Compose carries the documented network and resource limits."""
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text()

    for setting in (
        "SNS_MEDIA_TOKEN_TTL_SECONDS",
        "SNS_MEDIA_TOKEN_CAPACITY",
        "SNS_MEDIA_EXTRACTION_TIMEOUT_SECONDS",
        "SNS_MEDIA_EXTRACTION_BODY_LIMIT_BYTES",
        "SNS_MEDIA_MAX_DOWNLOAD_BYTES",
        "SNS_MEDIA_MEDIA_RESPONSE_TIMEOUT_SECONDS",
        "SNS_MEDIA_MAX_REDIRECTS",
        "SNS_MEDIA_MAX_EXTRACTIONS",
        "SNS_MEDIA_MAX_DOWNLOADS",
        "SNS_MEDIA_MAX_DOWNLOADS_PER_CLIENT",
        "SNS_MEDIA_RATE_LIMIT_WINDOW_SECONDS",
        "SNS_MEDIA_RATE_LIMIT_EXTRACTION_ATTEMPTS",
        "SNS_MEDIA_RATE_LIMIT_MEDIA_ATTEMPTS",
        "SNS_MEDIA_RATE_LIMIT_IDENTITY_CAPACITY",
        "SNS_MEDIA_GENERATED_PREVIEWS_ENABLED",
        "SNS_MEDIA_THUMBNAIL_INPUT_BYTES",
        "SNS_MEDIA_THUMBNAIL_OUTPUT_BYTES",
        "SNS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS",
        "SNS_MEDIA_THUMBNAIL_CONCURRENCY",
        "SNS_MEDIA_THUMBNAIL_CACHE_BYTES",
        "SNS_MEDIA_THUMBNAIL_MAX_EDGE",
    ):
        assert setting in compose


def test_platform_auth_overrides_mount_independent_read_only_cookie_files() -> None:
    """Verify each optional platform override exposes only a fixed read-only path."""
    overrides = {
        "docker-compose.instagram-auth.yaml": (
            "SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE",
            "SNS_MEDIA_INSTAGRAM_COOKIE_FILE",
            "/run/secrets/instagram.cookies.txt",
        ),
        "docker-compose.x-auth.yaml": (
            "SNS_MEDIA_X_COOKIE_HOST_FILE",
            "SNS_MEDIA_X_COOKIE_FILE",
            "/run/secrets/x-cookies.txt",
        ),
    }

    for filename, required_values in overrides.items():
        override = (PROJECT_ROOT / filename).read_text()
        for value in required_values:
            assert value in override
        assert "read_only: true" in override
        assert "session-cookie-value" not in override


def test_default_compose_has_no_platform_cookie_values_or_secret_mounts() -> None:
    """Verify anonymous deployment remains free of credential material and mounts."""
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text()

    assert "COOKIE_FILE" not in compose
    assert "/run/secrets" not in compose
    assert "session-cookie-value" not in compose


def test_nginx_example_authenticates_and_suppresses_sensitive_logs() -> None:
    """Verify the reverse-proxy example gates access and never logs token routes."""
    nginx = (PROJECT_ROOT / "deploy" / "nginx" / "sns-media-list.conf").read_text()

    assert "auth_basic" in nginx
    assert "auth_basic_user_file" in nginx
    assert "location ^~ /api/media/" in nginx
    assert "access_log off;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert 'proxy_set_header Forwarded "";' in nginx
    assert 'proxy_set_header Cookie "";' in nginx
    assert 'proxy_set_header Authorization "";' in nginx
    assert "proxy_buffering off;" in nginx
    assert "client_max_body_size 4k;" in nginx


def test_nginx_syntax_check_has_local_and_pinned_container_paths() -> None:
    """Verify Nginx syntax validation is repeatable without an installed binary."""
    checker = (PROJECT_ROOT / "scripts" / "check_nginx_config.py").read_text()

    assert '"nginx", "-t"' in checker
    assert "docker" in checker
    assert "nginx@sha256:" in checker


def test_stack_cookie_setting_matches_the_mounted_filename() -> None:
    """Verify the deployment stack uses the dot-separated Instagram cookie filename."""
    stack = (PROJECT_ROOT / "stack").read_text()

    assert "SNS_MEDIA_INSTAGRAM_COOKIE_FILE: /run/secrets/instagram.cookies.txt" in stack
    assert "SNS_MEDIA_INSTAGRAM_COOKIE_FILE: /run/secrets/instagram-cookies.txt" not in stack


def test_container_smoke_script_checks_runtime_boundaries() -> None:
    """Verify the automated smoke command covers startup and isolation checks."""
    smoke_script = (PROJECT_ROOT / "scripts" / "container_smoke.py").read_text()

    for check in (
        '"config", "--quiet"',
        '"up", "-d"',
        "State.Health.Status",
        "expected 10001",
        '"ffmpeg", "-version"',
        "5.1.9",
        '"uvicorn", "--version"',
        "read-only root filesystem check failed",
        "NetworkSettings.Ports",
        "CapDrop",
        "no-new-privileges",
        "PidsLimit",
        "restart-marker",
        '"stop", "-t", "10"',
    ):
        assert check in smoke_script
