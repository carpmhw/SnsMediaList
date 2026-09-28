"""Documentation contract tests for the Traditional Chinese operations guide."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_operations_guide_preserves_translated_safety_and_commands() -> None:
    """Verify translated operator guidance retains critical deployment contracts."""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")

    for required_text in (
        "# SNS Media List 操作指南",
        "## 部署",
        "## 升級與 rollback",
        "## Reverse proxy logging",
        "## Trusted proxy",
        "## 匿名平台限制",
        "## 故障排除",
        "## 自動化檢查",
        "## Owner-controlled manual smoke tests",
        "docker compose build --pull",
        "127.0.0.1:${SNS_MEDIA_HOST_PORT:-8000}:8000",
        "SNS_MEDIA_EXTRACTION_BODY_LIMIT_BYTES",
        "SNS_MEDIA_RATE_LIMIT_EXTRACTION_ATTEMPTS",
        "SNS_MEDIA_GENERATED_PREVIEWS_ENABLED",
        "no-new-privileges",
        "scripts/security_gate.py",
        "vulnerability-exceptions.json",
        "generated preview",
        "access_log off",
        "SNS_MEDIA_TRUSTED_PROXY_CIDRS",
        "deploy/nginx/sns-media-list.conf",
        "uv run python scripts/container_smoke.py",
        "uv run python scripts/check_nginx_config.py",
        "--instagram-image",
        "不會接受平台 Cookie 或 credentials",
        "不得記錄 token-bearing application path",
    ):
        assert required_text in guide


def test_operations_guide_documents_story_scope_and_trusted_session_risk() -> None:
    """Verify exact Story scope and operator-session visibility stay explicit."""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    cookie_section = guide.partition("## 平台 Cookie 驗證")[2].partition("\n## ")[0]
    anonymous_section = guide.partition("## 匿名平台限制")[2].partition("\n## ")[0]

    assert "`/stories/<username>/<numeric-media-id>/`" in anonymous_section
    assert "best effort" in anonymous_section
    assert re.search(r"帳號(?:全部|範圍).*Stories", anonymous_section)
    assert "Highlights" in anonymous_section

    assert re.search(r"任何.*服務使用者.*間接使用.*operator Instagram", cookie_section)
    for audience in ("私人", "Close Friends", "受眾限定"):
        assert audience in cookie_section
    assert "可信網路" in cookie_section
    assert re.search(r"低權限.*專用帳號", cookie_section)


def test_operations_guide_documents_cookie_lifecycle_and_story_error_split() -> None:
    """驗證 Cookie lifecycle 與 Story 錯誤診斷分類文件。"""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    cookie_section = guide.partition("## 平台 Cookie 驗證")[2].partition("\n## ")[0]
    diagnostics = guide.partition("## Instagram Story Extraction Diagnostics")[2].partition(
        "\n## "
    )[0]
    troubleshooting = guide.partition("## 故障排除")[2].partition("\n## ")[0]

    assert re.search(r"新.*extractor process.*(?:立即|重新)讀取", cookie_section)
    assert re.search(r"短效 token.*不含 Cookie", cookie_section)
    assert "到期" in cookie_section
    assert "重新啟動" in cookie_section
    assert re.search(r"CDN.*(?:不會|不得).*Cookie", cookie_section)

    session_failure = re.search(
        r"^\| `platform_authentication_failed`[^\n]+$", diagnostics, re.MULTILINE
    )
    story_auth_required = re.search(r"^\| `story_auth_required`[^\n]+$", diagnostics, re.MULTILINE)
    story_unavailable = re.search(r"^\| `story_unavailable`[^\n]+$", diagnostics, re.MULTILINE)
    ambiguous_refusal = re.search(r"^\| `extraction_failed`[^\n]+$", diagnostics, re.MULTILINE)
    assert session_failure is not None
    assert story_auth_required is not None
    assert story_unavailable is not None
    assert ambiguous_refusal is not None

    assert "503" in session_failure.group()
    assert "明確" in session_failure.group()
    for diagnostic in ("AuthRequired", "AuthenticationError", "invalid/expired", "challenge"):
        assert diagnostic in session_failure.group()

    assert "403" in story_auth_required.group()
    assert "未配置" in story_auth_required.group()
    assert "404" in story_unavailable.group()
    assert "過期" in story_unavailable.group()
    assert "429" in diagnostics and "504" in diagnostics and "502" in diagnostics
    assert "401/403" in ambiguous_refusal.group()
    assert "系統不細分實際原因" in diagnostics
    assert "配置 Cookie 本身不表示 session 已驗證有效" in diagnostics
    for diagnostic_field in (
        "extractor_diagnostic_source",
        "extractor_error_type",
        "extractor_exit_code",
        "extractor_http_statuses",
    ):
        assert f"`{diagnostic_field}`" in diagnostics
    for error_type in (
        "auth_required",
        "authentication_error",
        "authorization_error",
        "not_found",
        "http_error",
        "challenge_error",
        "extraction_error",
        "no_extractor",
    ):
        assert f"`{error_type}`" in diagnostics
    assert "Stderr 沒有結構化 type，固定為 `unknown`" in diagnostics
    assert "最多保留最小 8 個" in diagnostics
    assert "upstream HTTP failure" in diagnostics
    assert "本服務回傳的 HTTP 502" in diagnostics
    assert '"extractor_http_statuses":[500]' in diagnostics
    assert "docker compose logs --no-log-prefix --since 10m app" in diagnostics
    assert "--no-access-log" in diagnostics
    assert "extraction_failed" in diagnostics

    for stage in (
        "extractor_start",
        "extractor_timeout",
        "extractor_output_limit",
        "extractor_io",
        "extractor_process_unclassified",
        "extractor_invalid_output",
        "extractor_empty_output",
        "extractor_no_media",
        "extractor_platform_error",
        "unknown",
    ):
        assert f"`{stage}`" in diagnostics
    for required_text in (
        "`reason_code`",
        "`failure_stage`",
        "literal `[]`",
        "零 bytes",
        "成功退出",
        "uv run python scripts/verify_gallery_contract.py",
        "CONNECT proxy",
        "raw stderr／stdout",
    ):
        assert required_text in diagnostics

    assert "anonymous retry" in troubleshooting
    assert re.search(r"不暴露.*session.*細節", troubleshooting)


def test_operations_guide_documents_instagram_extractor_compatibility() -> None:
    """驗證通用相容性排錯、版本來源與 candidate gate，不綁定歷史升級紀錄。"""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "## Instagram Extractor Compatibility" in guide
    compatibility = guide.partition("## Instagram Extractor Compatibility")[2].partition("\n## ")[0]

    for required_text in (
        "零退出",
        "source pin",
        "service app is not running",
        "com.docker.compose.project",
        "com.docker.compose.service",
        "docker ps --filter label=com.docker.compose.service=app",
        "docker inspect --format",
        "docker logs --since 10m '<container>'",
        "git rev-parse HEAD",
        "git status --short",
        "docker image inspect --format",
        "Cookie 檔案存在、同步、格式／權限檢查通過",
        "gallery-dl --version",
        "`uv run python scripts/verify_gallery_contract.py`",
        "`pyproject.toml`",
        "`uv.lock`",
        "`Dockerfile`",
        "release notes",
        "Deterministic fake-extractor／container",
        "owner-controlled live Story 重試",
        "classifier",
        "#升級與-rollback",
        "#自動化檢查",
    ):
        assert required_text in compatibility

    assert compatibility.index("gallery-dl --version") < compatibility.index(
        "verify_gallery_contract.py"
    )
    assert compatibility.index("verify_gallery_contract.py") < compatibility.index("release notes")
    assert compatibility.index("release notes") < compatibility.index("candidate image")

    upgrade = guide.partition("## 升級與 rollback")[2].partition("\n## ")[0]
    checks = guide.partition("## 自動化檢查")[2].partition("\n## ")[0]
    for required_text in ("唯一 tag", "image ID／digest", "不可變", "rollback reference"):
        assert required_text in upgrade
    assert "另一個 smoke image 通過不能代替 candidate 驗證" in upgrade
    for required_text in (
        "uv run python scripts/security_gate.py --image",
        "HIGH／CRITICAL",
        "vulnerability-exceptions.json",
        "每次專用 image tag",
    ):
        assert required_text in checks


def test_operations_guide_documents_ephemeral_story_smoke_safety() -> None:
    """Verify live Story smoke input and output remain ephemeral and secret-safe."""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    smoke_section = guide.partition("## Owner-controlled manual smoke tests")[2].partition("\n## ")[
        0
    ]

    assert re.search(r"owner-controlled.*Story URL.*當下有效", smoke_section)
    for destination in ("repository", "CI", "log", "shell command history"):
        assert destination in smoke_section
    assert "24 小時" in smoke_section
    assert "release gate" in smoke_section
    assert re.search(r"case label.*status.*item count.*outcome", smoke_section)
    for secret in ("URL", "token", "Cookie"):
        assert re.search(rf"不得記錄[^。\n]*{secret}", smoke_section)


def test_operations_guide_documents_optional_story_file_workflow() -> None:
    """Verify the optional Story smoke file is private, ephemeral, and non-CI."""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    smoke_section = guide.partition("## Owner-controlled manual smoke tests")[2].partition("\n## ")[
        0
    ]

    for required_text in (
        "mktemp /tmp/",
        "chmod 600",
        "read -r",
        "trap",
        "rm -f",
        "--instagram-story-file",
        "選用",
        "CI",
        "release gate",
    ):
        assert required_text in smoke_section
    assert re.search(r"repository.*(?:外|之外)", smoke_section)
    assert "尚未提供 Story 參數" not in smoke_section
    assert re.search(r"--instagram-story(?:[ =]|$)", smoke_section) is None


def test_operations_guide_keeps_batch_ui_within_existing_limits() -> None:
    """驗證操作文件不把前端批次功能誤導為提高伺服器併發的理由。"""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")

    for required_text in (
        "batch UI 不會改變 `SNS_MEDIA_MAX_EXTRACTIONS`",
        "不是 server job queue",
        "現有 download limits 與 rate limit",
        "不得因 UI 批次功能任意提高 concurrency",
        "已開始下載",
        "不代表瀏覽器或 OS 已完成檔案保存",
    ):
        assert required_text in guide
