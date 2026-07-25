"""Documentation contract tests for the project README."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_readme_documents_supported_operation_and_limits() -> None:
    """Verify the README preserves critical setup, safety, and license guidance."""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for required_text in (
        "# SNS Media List",
        "docker compose up -d --build",
        "SNS_MEDIA_HOST_PORT",
        "uv sync --extra dev",
        "uv run uvicorn sns_media_list.app:create_app --factory",
        "公開可見不代表匿名模式一定能存取",
        "不支援私人貼文",
        "OPERATIONS.md",
        "gallery-dl",
        "GPL-2.0-only",
        "MIT License",
    ):
        assert required_text in readme


def test_readme_documents_single_story_support_and_security_boundaries() -> None:
    """Verify the README documents exact Story support and trusted-session risks."""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    support_section = readme.partition("## 支援範圍與限制")[2].partition("\n## ")[0]
    cookie_section = readme.partition("## 平台 Cookie 驗證（可信部署）")[2].partition("\n## ")[0]
    error_section = readme.partition("## 常見錯誤")[2].partition("\n## ")[0]

    story_support_row = re.search(
        r"^\|[^|\n]*`/stories/<username>/<numeric-media-id>/`[^|\n]*"
        r"\|\s*支援[^|\n]*\|$",
        support_section,
        re.MULTILINE,
    )
    assert story_support_row is not None
    assert "匿名" in story_support_row.group()
    assert "best effort" in story_support_row.group()
    assert re.search(
        r"^\|[^|\n]*帳號全部 Stories[^|\n]*Highlights[^|\n]*\|\s*不支援\s*\|$",
        support_section,
        re.MULTILINE,
    )

    assert re.search(r"Story[^|\n]*主要圖片[^|\n]*漸進式影片", support_section)
    assert re.search(r"圖片 Story[^。\n]*音訊[^。\n]*audio", support_section)
    assert re.search(r"不提供[^。\n]*Story 批次[^。\n]*ZIP[^。\n]*轉碼", support_section)

    assert re.search(
        r"operator[^。\n]*Story[^。\n]*私人[^。\n]*Close Friends[^。\n]*受眾限定",
        cookie_section,
    )
    assert "低權限" in cookie_section
    assert "可信內網" in cookie_section

    story_error_row = re.search(
        r"^\|\s*`story_unavailable`\s*\|\s*404\s*\|[^|\n]*\|$",
        error_section,
        re.MULTILINE,
    )
    assert story_error_row is not None
    assert "不細分" in story_error_row.group()
    assert "推斷" in story_error_row.group()
