"""Check that every Python function and method has a useful docstring."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable, Sequence
from pathlib import Path

Finding = tuple[str, int, str]


def find_missing_docstrings(path: Path) -> list[Finding]:
    """Return source locations for functions without docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and ast.get_docstring(node) is None
        ):
            findings.append((str(path), node.lineno, node.name))
    return findings


def iter_python_files(paths: Iterable[Path]) -> Iterable[Path]:
    """Yield deterministic Python files from the requested files and directories."""
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(path.rglob("*.py"))
        elif path.suffix == ".py":
            files.add(path)
    yield from sorted(files)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the docstring check and return a shell-friendly status code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    options = parser.parse_args(arguments)
    findings: list[Finding] = []
    for path in iter_python_files(options.paths):
        findings.extend(find_missing_docstrings(path))
    for filename, line, name in findings:
        print(f"{filename}:{line}: function {name!r} has no docstring")
    if not findings:
        print("All Python functions have docstrings.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
