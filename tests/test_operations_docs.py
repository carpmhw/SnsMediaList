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
    """Verify Cookie lifecycle and the configured Story 404/503 distinction."""
    guide = (PROJECT_ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    cookie_section = guide.partition("## 平台 Cookie 驗證")[2].partition("\n## ")[0]
    troubleshooting = guide.partition("## 故障排除")[2].partition("\n## ")[0]

    assert re.search(r"新.*extractor process.*(?:立即|重新)讀取", cookie_section)
    assert re.search(r"短效 token.*不含 Cookie", cookie_section)
    assert "到期" in cookie_section
    assert "重新啟動" in cookie_section
    assert re.search(r"CDN.*(?:不會|不得).*Cookie", cookie_section)

    session_failure = re.search(
        r"^- `platform_authentication_failed`[^\n]+$", troubleshooting, re.MULTILINE
    )
    story_unavailable = re.search(r"^- `story_unavailable`[^\n]+$", troubleshooting, re.MULTILINE)
    assert session_failure is not None
    assert story_unavailable is not None

    assert "503" in session_failure.group()
    assert re.search(r"(?:只|僅).*明確.*session", session_failure.group())
    for diagnostic in ("invalid/expired", "login", "challenge", "consent", "redirect"):
        assert diagnostic in session_failure.group()

    assert "404" in story_unavailable.group()
    assert "configured Story" in story_unavailable.group()
    assert "AuthRequired" in story_unavailable.group()
    assert "HTTP 401/403/404" in story_unavailable.group()
    assert re.search(r"無法可靠區分.*session 有效但不可見.*availability", story_unavailable.group())

    assert re.search(r"兩種情況.*不會.*anonymous retry", troubleshooting)
    assert re.search(r"不暴露.*session.*細節", troubleshooting)


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
