"""Select bounded project context and format failures for the repair model."""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ContextSelection:
    """Explain why one bounded file was included in model context."""

    path: str
    reason: str
    is_test: bool
    chars: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        """Return stable, content-free audit metadata."""

        return {
            "path": self.path,
            "reason": self.reason,
            "is_test": self.is_test,
            "chars": self.chars,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class SourceContext:
    """Bounded model files plus auditable selection metadata."""

    files: dict[str, str]
    selections: tuple[ContextSelection, ...]


def _is_source(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_SUFFIXES or path.name in CONFIG_NAMES


def _is_test_source(root: Path, path: Path) -> bool:
    """Return whether a canonical project-relative path is a test source."""

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
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and _inside(root, candidate)
            and _is_source(candidate)
        ):
            resolved = candidate.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
    return candidates


def _project_source_paths(root: Path) -> list[Path]:
    """Enumerate project sources without descending into ignored trees."""

    sources: list[Path] = []
    seen: set[Path] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_PARTS and not (parent / name).is_symlink()
        )
        for name in sorted(filenames):
            path = parent / name
            if path.is_symlink() or not _is_source(path):
                continue
            resolved = path.resolve()
            if _inside(root, resolved) and resolved not in seen:
                seen.add(resolved)
                sources.append(resolved)
    return sources


def _local_import_specs(path: Path, text: str) -> list[tuple[str, str]]:
    """Return conservative local import specs and their resolution style."""

    specs: list[tuple[str, str]] = []
    suffix = path.suffix.lower()
    if suffix == ".py":
        pattern = re.compile(
            r"^\s*(?:from\s+(?P<from>[.A-Za-z_][.\w]*)\s+import"
            r"|import\s+(?P<import>[A-Za-z_]\w*(?:\.\w+)*))",
            re.MULTILINE,
        )
        for match in pattern.finditer(text):
            specs.append((match.group("from") or match.group("import"), "python"))
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        pattern = re.compile(
            r"(?:\bfrom\s*|\brequire\s*\(|\bimport\s*)[\"']"
            r"(?P<path>\.{1,2}/[^\"']+)[\"']"
        )
        specs.extend(
            (match.group("path"), "javascript") for match in pattern.finditer(text)
        )
    elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
        pattern = re.compile(r'^\s*#\s*include\s*"(?P<path>[^"]+)"', re.MULTILINE)
        specs.extend((match.group("path"), "include") for match in pattern.finditer(text))
    return specs


def _resolve_local_import(
    root: Path,
    importer: Path,
    spec: str,
    style: str,
    project_sources: set[Path],
) -> Path | None:
    """Resolve a local import only when it maps to a known project source."""

    candidates: list[Path] = []
    leading = 0
    module = ""
    if style == "python":
        leading = len(spec) - len(spec.lstrip("."))
        module = spec[leading:]
        base = importer.parent if leading else root
        for _ in range(max(0, leading - 1)):
            base = base.parent
        module_path = base.joinpath(*module.split(".")) if module else base
        candidates.extend((module_path.with_suffix(".py"), module_path / "__init__.py"))
    elif style == "javascript":
        base = importer.parent / spec
        candidates.append(base)
        suffixes = (".js", ".jsx", ".ts", ".tsx")
        candidates.extend(base.with_suffix(suffix) for suffix in suffixes)
        candidates.extend(base / f"index{suffix}" for suffix in suffixes)
    else:
        candidates.append(importer.parent / spec)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in project_sources and _inside(root, resolved):
            return resolved
    if style == "python" and not leading and module:
        # ``src/`` and similar package roots are common but cannot be assumed.
        # Accept a module-suffix match only when it identifies exactly one
        # enumerated project source; ambiguous layouts deliberately fall back.
        module_parts = tuple(module.split("."))
        matches: list[Path] = []
        for source in project_sources:
            relative = source.relative_to(root)
            source_parts = (
                relative.parent.parts
                if source.name == "__init__.py"
                else relative.with_suffix("").parts
            )
            if tuple(source_parts[-len(module_parts) :]) == module_parts:
                matches.append(source)
        if len(matches) == 1:
            return matches[0]
    return None


def collect_source_context_with_metadata(
    root: Path,
    build: BuildResult,
    *,
    max_files: int = 30,
    max_file_chars: int = 16_000,
    max_total_chars: int = 60_000,
) -> SourceContext:
    """Select direct diagnostics, their local imports, then fallback sources."""

    root = root.expanduser().resolve()
    output = "\n".join(f"{step.stdout}\n{step.stderr}" for step in build.steps)
    referenced = _referenced_paths(root, output)

    project_sources = _project_source_paths(root)
    source_set = set(project_sources)

    imported_by: dict[Path, Path] = {}
    import_queue = deque(referenced)
    inspected_importers: set[Path] = set()
    # Bound graph traversal independently of repository size. This is enough
    # for package re-exports while preventing generated import chains from
    # consuming unbounded time before the existing file/character budgets run.
    max_import_nodes = max(max_files * 4, max_files)
    while import_queue and len(inspected_importers) < max_import_nodes:
        importer = import_queue.popleft()
        if importer in inspected_importers:
            continue
        inspected_importers.add(importer)
        try:
            with importer.open("r", encoding="utf-8") as handle:
                import_text = handle.read(64_000)
        except (OSError, UnicodeDecodeError):
            continue
        for spec, style in _local_import_specs(importer, import_text):
            imported = _resolve_local_import(root, importer, spec, style, source_set)
            if imported is not None and imported != importer:
                imported_by.setdefault(imported, importer)
                if imported not in inspected_importers:
                    import_queue.append(imported)

    referenced_set = set(referenced)

    def priority(path: Path) -> tuple[int, str]:
        is_test = _is_test_source(root, path)
        if path in referenced_set and not is_test:
            group = 0
        elif path in imported_by and not is_test:
            group = 1
        elif path in referenced_set:
            group = 2
        elif path in imported_by:
            group = 3
        elif not is_test:
            group = 4
        else:
            group = 5
        return group, path.relative_to(root).as_posix()

    ordered = sorted(set(project_sources) | referenced_set, key=priority)
    collected: dict[str, str] = {}
    selections: list[ContextSelection] = []
    used = 0
    for path in ordered:
        if len(collected) >= max_files or used >= max_total_chars:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        remaining = max_total_chars - used
        limit = min(max_file_chars, remaining)
        clipped = text[:limit]
        if not clipped:
            continue
        relative = path.relative_to(root).as_posix()
        if path in referenced_set:
            reason = "build-output"
        elif path in imported_by:
            importer = imported_by[path].relative_to(root).as_posix()
            reason = f"local-import:{importer}"
        else:
            reason = "project-source-fallback"
        collected[relative] = clipped
        selections.append(
            ContextSelection(
                relative,
                reason,
                _is_test_source(root, path),
                len(clipped),
                len(text) > len(clipped),
            )
        )
        used += len(clipped)
    return SourceContext(collected, tuple(selections))


def collect_source_context(
    root: Path,
    build: BuildResult,
    *,
    max_files: int = 30,
    max_file_chars: int = 16_000,
    max_total_chars: int = 60_000,
) -> dict[str, str]:
    """Return the compatible file mapping from ranked bounded context.

    Generated directories and symlinks are excluded so context stays inside the
    reviewed project tree and does not accidentally include dependency caches.
    """

    return collect_source_context_with_metadata(
        root,
        build,
        max_files=max_files,
        max_file_chars=max_file_chars,
        max_total_chars=max_total_chars,
    ).files


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

