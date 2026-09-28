"""Contract tests for the hardened container deployment."""

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_dockerfile_uses_pinned_runtime_and_non_root_entrypoint() -> None:
    """確認 image 固定 Trixie base／FFmpeg 版本，安裝 locked app 並以 app 執行。"""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert (
        "python:3.12-slim-trixie@sha256:"
        "f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
    ) in dockerfile
    assert "ARG FFMPEG_VERSION=7:7.1.5-0+deb13u1" in dockerfile
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
    """確認 Debian Trixie FFmpeg runtime 的授權 notice 已隨 image 提供。"""
    notice = (PROJECT_ROOT / "LICENSES" / "ffmpeg.txt").read_text()

    assert "FFmpeg" in notice
    assert "Debian Trixie" in notice
    assert "7:7.1.5-0+deb13u1" in notice
    assert "GPL-2.0" in notice
    assert "ffmpeg.org" in notice


def test_gallery_license_notice_is_shipped() -> None:
    """確認授權 notice 版本與專案精確 pin 一致並納入 image context。"""
    notice = (PROJECT_ROOT / "LICENSES" / "gallery-dl.txt").read_text()
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [
        dependency.removeprefix("gallery-dl==")
        for dependency in project["project"]["dependencies"]
        if isinstance(dependency, str) and dependency.startswith("gallery-dl==")
    ]

    assert len(pins) == 1
    assert f"gallery-dl {pins[0]}" in notice
    assert f"gallery-dl version {pins[0]}" in notice
    assert f"/v{pins[0]}" in notice
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
    assert 'proxy_set_header Proxy-Authorization "";' in nginx
    assert "proxy_hide_header Set-Cookie;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "proxy_read_timeout 300s;" in nginx
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


def test_story_diagnostics_smoke_uses_external_readonly_fixtures() -> None:
    """驗證 Story diagnostics smoke 使用唯讀 fake inputs 且不開 access log。"""
    smoke_script = (PROJECT_ROOT / "scripts" / "container_story_diagnostics_smoke.py").read_text()

    for required in (
        '"SNS_MEDIA_STORY_DIAGNOSTICS_IMAGE"',
        'f"sns-media-list:story-diagnostics-{uuid.uuid4().hex[:12]}"',
        "read_only: true",
        'FAKE_GALLERY_PATH = "/opt/venv/bin/gallery-dl"',
        'COOKIE_CONTAINER_PATH = "/run/secrets/instagram-cookies.txt"',
        '"--no-access-log"',
        '"unknown-stderr"',
        '"unknown-datajob"',
        '"invalid-json"',
        '"configured-http-403"',
        '"story_auth_required"',
        '"platform_authentication_failed"',
        '"extractor_process_unclassified"',
        '"extractor_invalid_output"',
        '"extractor_platform_error"',
        '"failure_stage"',
        '"extractor_diagnostic_source"',
        '"extractor_error_type"',
        '"extractor_exit_code"',
        '"extractor_http_statuses"',
        '"message": "HTTP 403 Forbidden PRIVATE_CONTAINER_RAW_DIAGNOSTIC"',
        "extractor_diagnostics=",
        '"down", "--remove-orphans"',
    ):
        assert required in smoke_script


def test_container_smoke_script_checks_runtime_boundaries() -> None:
    """確認 container smoke 驗證啟動、隔離與 Trixie FFmpeg runtime。"""
    smoke_script = (PROJECT_ROOT / "scripts" / "container_smoke.py").read_text()

    for check in (
        '"config", "--quiet"',
        '"up", "-d"',
        "State.Health.Status",
        "expected 10001",
        '"ffmpeg", "-version"',
        "7.1.5-",
        '"uvicorn", "--version"',
        "read-only root filesystem check failed",
        "NetworkSettings.Ports",
        "CapDrop",
        "no-new-privileges",
        "PidsLimit",
        "restart-marker",
        '"stop", "-t", "10"',
        "container_story_diagnostics_smoke.py",
        "SNS_MEDIA_STORY_DIAGNOSTICS_IMAGE",
        "repository digests",
    ):
        assert check in smoke_script


def test_container_smoke_uses_a_per_run_image_tag() -> None:
    """確認 container smoke 使用獨立 image tag，不重標記共享 local image。"""
    compose = (PROJECT_ROOT / "docker-compose.yaml").read_text()
    smoke_script = (PROJECT_ROOT / "scripts" / "container_smoke.py").read_text()

    assert "image: sns-media-list:${SNS_MEDIA_IMAGE_TAG:-local}" in compose
    assert 'image_tag = f"smoke-{os.getpid()}"' in smoke_script
    assert 'environment["SNS_MEDIA_IMAGE_TAG"] = image_tag' in smoke_script
    assert 'image_reference = f"sns-media-list:{image_tag}"' in smoke_script
    assert '"docker",\n                "image",\n                "inspect"' in smoke_script
    assert 'diagnostic_environment["SNS_MEDIA_STORY_DIAGNOSTICS_IMAGE"] = image_reference' in (
        smoke_script
    )


def test_story_diagnostics_smoke_uses_the_same_isolated_per_run_image() -> None:
    """確認 Story 日誌矩陣使用 container smoke 的 per-run image reference。"""
    container_smoke = (PROJECT_ROOT / "scripts" / "container_smoke.py").read_text()
    diagnostics_smoke = (
        PROJECT_ROOT / "scripts" / "container_story_diagnostics_smoke.py"
    ).read_text()

    assert 'diagnostic_environment["SNS_MEDIA_STORY_DIAGNOSTICS_IMAGE"] = image_reference' in (
        container_smoke
    )
    assert "supplied_image = os.environ.get(_CANDIDATE_IMAGE_ENV)" in diagnostics_smoke
    assert "build_candidate=build_image and index == 0" in diagnostics_smoke
    assert 'CANDIDATE_IMAGE = "sns-media-list:story-diagnostics-candidate"' not in (
        diagnostics_smoke
    )
