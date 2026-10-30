"""Discover explicitly configured, locally available static analyzers."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix.models import BuildStep


@dataclass(frozen=True)
class StaticAnalyzer:
    """One project-declared analyzer and its fixed, shell-free command."""

    name: str
    language: str
    markers: tuple[str, ...]
    step: BuildStep
    available: bool
    availability: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize discovery without executing the analyzer."""

        return {
            "name": self.name,
            "language": self.language,
            "markers": list(self.markers),
            "step": self.step.to_dict(),
            "available": self.available,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class StaticAnalysisInspection:
    """All explicitly declared analyzers found at one project root."""

    root: Path
    analyzers: tuple[StaticAnalyzer, ...]

    @property
    def available_steps(self) -> tuple[BuildStep, ...]:
        """Return only commands whose executable is already available."""

        return tuple(item.step for item in self.analyzers if item.available)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable discovery envelope for CLI and future plans."""

        return {
            "root": str(self.root),
            "analyzers": [item.to_dict() for item in self.analyzers],
            "available_count": sum(item.available for item in self.analyzers),
            "declared_count": len(self.analyzers),
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dependency_declared(data: object, package: str) -> bool:
    """Find a package name in standard dependency strings and mappings."""

    normalized = package.lower().replace("_", "-")
    if isinstance(data, dict):
        return any(
            str(key).lower().replace("_", "-") == normalized
            or _dependency_declared(value, package)
            for key, value in data.items()
        )
    if isinstance(data, list):
        return any(_dependency_declared(item, package) for item in data)
    if isinstance(data, str):
        name = data.split(";", 1)[0].strip().split("[", 1)[0]
        for separator in ("=", "<", ">", "!", "~", " "):
            name = name.split(separator, 1)[0]
        return name.lower().replace("_", "-") == normalized
    return False


def _is_runnable_file(path: Path) -> bool:
    """Require an executable bit on POSIX and a regular file on Windows."""

    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _path_tool(root: Path, name: str) -> tuple[str | None, str]:
    """Check project virtual environments and PATH without installing tools."""

    if shutil.which(name):
        return name, "PATH"
    local_candidates = (
        root / ".venv" / "Scripts" / f"{name}.exe",
        root / ".venv" / "bin" / name,
    )
    local = next((path for path in local_candidates if _is_runnable_file(path)), None)
    if local is not None:
        return local.relative_to(root).as_posix(), "project-venv"
    return None, "executable not found in project .venv or PATH"


def _python_analyzers(root: Path) -> list[StaticAnalyzer]:
    pyproject = root / "pyproject.toml"
    data = _read_toml(pyproject) if pyproject.is_file() else {}
    tool = data.get("tool", {}) if isinstance(data.get("tool", {}), dict) else {}
    project = data.get("project", {})
    project = project if isinstance(project, dict) else {}
    poetry = tool.get("poetry", {})
    poetry = poetry if isinstance(poetry, dict) else {}
    dependency_data = {
        "dependencies": project.get("dependencies", []),
        "optional-dependencies": project.get("optional-dependencies", {}),
        "dependency-groups": data.get("dependency-groups", {}),
        "poetry-dependencies": poetry.get("dependencies", {}),
        "poetry-groups": poetry.get("group", {}),
    }
    declared_dependencies = {
        name: _dependency_declared(dependency_data, name)
        for name in ("ruff", "mypy")
    }
    analyzers: list[StaticAnalyzer] = []

    ruff_markers = tuple(
        name for name in ("ruff.toml", ".ruff.toml") if (root / name).is_file()
    )
    if pyproject.is_file() and ("ruff" in tool or declared_dependencies["ruff"]):
        ruff_markers = ("pyproject.toml", *ruff_markers)
    if ruff_markers:
        executable, reason = _path_tool(root, "ruff")
        analyzers.append(
            StaticAnalyzer(
                "ruff",
                "Python",
                ruff_markers,
                BuildStep(
                    "Run Ruff static analysis",
                    (executable or "ruff", "check", "."),
                ),
                executable is not None,
                reason,
            )
        )

    mypy_markers = tuple(
        name for name in ("mypy.ini", ".mypy.ini") if (root / name).is_file()
    )
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            if "[mypy" in setup_cfg.read_text(encoding="utf-8"):
                mypy_markers = (*mypy_markers, "setup.cfg")
        except (OSError, UnicodeDecodeError):
            pass
    if pyproject.is_file() and ("mypy" in tool or declared_dependencies["mypy"]):
        mypy_markers = ("pyproject.toml", *mypy_markers)
    if mypy_markers:
        executable, reason = _path_tool(root, "mypy")
        analyzers.append(
            StaticAnalyzer(
                "mypy",
                "Python",
                mypy_markers,
                BuildStep(
                    "Run mypy static analysis", (executable or "mypy", ".")
                ),
                executable is not None,
                reason,
            )
        )
    return analyzers


def _eslint_analyzer(root: Path) -> StaticAnalyzer | None:
    package_file = root / "package.json"
    if not package_file.is_file():
        return None
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(package, dict):
        return None
    scripts = package.get("scripts", {})
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        declared_dependencies = package.get(key, {})
        if isinstance(declared_dependencies, dict):
            dependencies.update(declared_dependencies)
    lint = scripts.get("lint") if isinstance(scripts, dict) else None
    config_markers = tuple(
        name
        for name in (
            "eslint.config.js",
            "eslint.config.mjs",
            "eslint.config.cjs",
            "eslint.config.ts",
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.js",
            ".eslintrc.cjs",
        )
        if (root / name).is_file()
    )
    has_lint_script = isinstance(lint, str) and bool(lint.strip())
    declared = "eslint" in dependencies or bool(config_markers) or (
        has_lint_script and "eslint" in lint.lower()
    )
    if not declared:
        return None
    manager = "npm"
    if (root / "pnpm-lock.yaml").is_file():
        manager = "pnpm"
    elif (root / "yarn.lock").is_file():
        manager = "yarn"
    executable = f"{manager}.cmd" if os.name == "nt" else manager
    manager_available = shutil.which(executable) is not None
    local_eslint = root / "node_modules" / ".bin" / (
        "eslint.cmd" if os.name == "nt" else "eslint"
    )
    local_eslint_available = _is_runnable_file(local_eslint)
    available = local_eslint_available and (manager_available or not has_lint_script)
    if has_lint_script and not manager_available:
        availability = f"{executable} not found in PATH"
    elif not local_eslint_available:
        availability = "local node_modules/.bin/eslint not found"
    else:
        availability = "project node_modules"
    if has_lint_script:
        argv = (
            (executable, "lint")
            if manager == "yarn"
            else (executable, "run", "lint")
        )
    else:
        argv = (local_eslint.relative_to(root).as_posix(), ".")
    return StaticAnalyzer(
        "eslint",
        "JavaScript/TypeScript",
        ("package.json", *config_markers),
        BuildStep(
            "Run declared ESLint script"
            if has_lint_script
            else "Run ESLint static analysis",
            argv,
        ),
        available,
        availability,
    )


def _clippy_analyzer(root: Path) -> StaticAnalyzer | None:
    cargo_file = root / "Cargo.toml"
    if not cargo_file.is_file():
        return None
    data = _read_toml(cargo_file)
    lints = data.get("lints", {}) if isinstance(data.get("lints", {}), dict) else {}
    workspace = data.get("workspace", {})
    workspace = workspace if isinstance(workspace, dict) else {}
    workspace_lints = workspace.get("lints", {})
    workspace_lints = workspace_lints if isinstance(workspace_lints, dict) else {}
    markers = tuple(
        name for name in ("clippy.toml", ".clippy.toml") if (root / name).is_file()
    )
    if isinstance(lints.get("clippy"), dict) or isinstance(
        workspace_lints.get("clippy"), dict
    ):
        markers = ("Cargo.toml", *markers)
    if not markers:
        return None
    cargo_available = shutil.which("cargo") is not None
    clippy_available = shutil.which("cargo-clippy") is not None
    available = cargo_available and clippy_available
    availability = (
        "PATH"
        if available
        else "cargo and cargo-clippy must both be available in PATH"
    )
    return StaticAnalyzer(
        "clippy",
        "Rust",
        markers,
        BuildStep("Run Clippy static analysis", ("cargo", "clippy", "--all-targets")),
        available,
        availability,
    )


def inspect_static_analysis(path: Path | str) -> StaticAnalysisInspection:
    """Discover declared analyzers without executing or installing anything."""

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Project directory does not exist: {root}")
    analyzers = _python_analyzers(root)
    eslint = _eslint_analyzer(root)
    clippy = _clippy_analyzer(root)
    if eslint is not None:
        analyzers.append(eslint)
    if clippy is not None:
        analyzers.append(clippy)
    return StaticAnalysisInspection(root, tuple(analyzers))
