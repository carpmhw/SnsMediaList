"""Documentation contract tests for the project README."""

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
