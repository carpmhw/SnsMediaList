"""Tests for the repository function-docstring verification script."""

from pathlib import Path

from scripts.check_function_docstrings import find_missing_docstrings


def test_checker_reports_functions_without_docstrings(tmp_path: Path) -> None:
    """Verify the checker reports the source location and function name."""
    source = tmp_path / "missing.py"
    source.write_text("def missing():\n    return None\n")

    findings = find_missing_docstrings(source)

    assert findings == [(str(source), 1, "missing")]


def test_checker_accepts_documented_sync_and_async_functions(tmp_path: Path) -> None:
    """Verify documented functions and methods are accepted."""
    source = tmp_path / "documented.py"
    source.write_text(
        'async def documented():\n    """Documented."""\n\n'
        'class Example:\n    def method(self):\n        """Documented."""\n'
    )

    assert find_missing_docstrings(source) == []
