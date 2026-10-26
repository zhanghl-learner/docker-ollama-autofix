"""Select bounded project context and format failures for the repair model."""

from __future__ import annotations

import re
from pathlib import Path

from autofix.builders import BuildResult


SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}
CONFIG_NAMES = {
    "CMakeLists.txt",
    "Cargo.toml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}
IGNORED_PARTS = {
    ".autofix",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}


def _is_source(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_SUFFIXES or path.name in CONFIG_NAMES


def _is_test_source(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    name = relative.name.lower()
    stem = relative.stem.lower()
    return (
        any(part.lower() in {"test", "tests", "spec", "specs", "__tests__"} for part in relative.parts[:-1])
        or name.startswith(("test_", "test-"))
        or stem.endswith(("_test", "-test", ".test", "_spec", "-spec", ".spec"))
    )


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _referenced_paths(root: Path, output: str) -> list[Path]:
    """Extract only existing, project-contained source paths from build output."""

    candidates: list[Path] = []
    pattern = re.compile(r"(?P<path>[A-Za-z0-9_. /\\-]+\.[A-Za-z0-9]+)(?::\d+)?")
    for match in pattern.finditer(output):
        raw = match.group("path").strip().replace("\\", "/")
        path = Path(raw)
        candidate = path if path.is_absolute() else root / path
        if candidate.is_file() and _inside(root, candidate) and _is_source(candidate):
            resolved = candidate.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
    return candidates


def collect_source_context(
    root: Path,
    build: BuildResult,
    *,
    max_files: int = 30,
    max_file_chars: int = 16_000,
    max_total_chars: int = 60_000,
) -> dict[str, str]:
    """Collect referenced files first, then small project sources within limits.

    Generated directories and symlinks are excluded so context stays inside the
    reviewed project tree and does not accidentally include dependency caches.
    """

    output = "\n".join(f"{step.stdout}\n{step.stderr}" for step in build.steps)
    ordered = _referenced_paths(root, output)
    for path in sorted(root.rglob("*")):
        if len(ordered) >= max_files:
            break
        if not path.is_file() or not _is_source(path):
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        resolved = path.resolve()
        if _inside(root, resolved) and resolved not in ordered:
            ordered.append(resolved)

    # Put production sources before protected tests so small models focus on the
    # implementation while still receiving tests as behavioral context.
    ordered.sort(key=lambda path: _is_test_source(root, path))

    collected: dict[str, str] = {}
    used = 0
    for path in ordered:
        if len(collected) >= max_files or used >= max_total_chars:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        remaining = max_total_chars - used
        clipped = text[: min(max_file_chars, remaining)]
        if not clipped:
            continue
        collected[path.relative_to(root).as_posix()] = clipped
        used += len(clipped)
    return collected


def format_failure(build: BuildResult, max_chars: int = 24_000) -> str:
    """Render executed-step diagnostics and keep the most recent output."""

    chunks: list[str] = []
    for step in build.steps:
        chunks.append(
            f"Command: {' '.join(step.argv)}\n"
            f"Exit code: {step.exit_code}\n"
            f"STDOUT:\n{step.stdout}\nSTDERR:\n{step.stderr}"
        )
    return "\n\n".join(chunks)[-max_chars:]

