"""Documentation contract tests for the Traditional Chinese operations guide."""

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
        "SNS_MEDIA_TRUSTED_PROXY_CIDRS",
        "~^/api/media/[^/?]+/(?:preview|download)(?:\\?|$)",
        "uv run python scripts/container_smoke.py",
        "--instagram-image",
        "不會接受平台 Cookie 或 credentials",
        "不得記錄 token-bearing application path",
    ):
        assert required_text in guide
