from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from autofix.models import BuildStep, ProjectInspection, ProjectMatch


def _step(label: str, *argv: str) -> BuildStep:
    return BuildStep(label=label, argv=tuple(argv))


def _has_top_level_runtime(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return False
    passive = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in tree.body:
        if isinstance(node, passive):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict)):
                continue
        return True
    return False


def _python_entrypoint(root: Path) -> Path | None:
    sources = [
        path
        for path in sorted(root.glob("*.py"))
        if not path.name.startswith("test_") and not path.stem.endswith("_test")
    ]
    runnable = [path for path in sources if _has_top_level_runtime(path)]
    for preferred in ("main.py", "app.py", "run.py"):
        match = next((path for path in runnable if path.name == preferred), None)
        if match:
            return match
    return runnable[0] if len(runnable) == 1 else None


def _python_match(root: Path) -> ProjectMatch | None:
    markers = tuple(
        name
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "pytest.ini")
        if (root / name).is_file()
    )
    if not markers and not any(root.glob("*.py")):
        return None

    has_root_tests = any(root.glob("test_*.py")) or any(root.glob("*_test.py"))
    if (root / "tests").is_dir() or (root / "pytest.ini").is_file() or has_root_tests:
        steps = (_step("Run Python tests", "python", "-m", "pytest"),)
    elif entrypoint := _python_entrypoint(root):
        steps = (_step("Run Python entry point", "python", entrypoint.name),)
    else:
        steps = (_step("Compile Python sources", "python", "-m", "compileall", "-q", "."),)
    return ProjectMatch("Python", "python", markers or ("*.py",), steps)


def _cmake_match(root: Path) -> ProjectMatch | None:
    if not (root / "CMakeLists.txt").is_file():
        return None
    return ProjectMatch(
        "C/C++",
        "cmake",
        ("CMakeLists.txt",),
        (
            _step("Configure CMake", "cmake", "-S", ".", "-B", "build"),
            _step("Build CMake project", "cmake", "--build", "build"),
        ),
    )


def _maven_match(root: Path) -> ProjectMatch | None:
    if not (root / "pom.xml").is_file():
        return None
    executable = "mvnw.cmd" if os.name == "nt" and (root / "mvnw.cmd").is_file() else "mvn"
    if os.name != "nt" and (root / "mvnw").is_file():
        executable = "./mvnw"
    return ProjectMatch("Java", "maven", ("pom.xml",), (_step("Run Maven tests", executable, "test"),))


def _gradle_match(root: Path) -> ProjectMatch | None:
    markers = tuple(name for name in ("build.gradle", "build.gradle.kts") if (root / name).is_file())
    if not markers:
        return None
    executable = "gradlew.bat" if os.name == "nt" and (root / "gradlew.bat").is_file() else "gradle"
    if os.name != "nt" and (root / "gradlew").is_file():
        executable = "./gradlew"
    return ProjectMatch("Java/Kotlin", "gradle", markers, (_step("Run Gradle tests", executable, "test"),))


def _node_match(root: Path) -> ProjectMatch | None:
    package_file = root / "package.json"
    if not package_file.is_file():
        return None

    package_manager = "npm"
    if (root / "pnpm-lock.yaml").is_file():
        package_manager = "pnpm"
    elif (root / "yarn.lock").is_file():
        package_manager = "yarn"

    script = "test"
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        if "test" not in scripts and "build" in scripts:
            script = "build"
    except (OSError, json.JSONDecodeError):
        pass

    executable = f"{package_manager}.cmd" if os.name == "nt" else package_manager
    argv = (executable, script) if package_manager == "yarn" else (executable, "run", script)
    return ProjectMatch(
        "JavaScript/TypeScript",
        package_manager,
        ("package.json",),
        (_step(f"Run {package_manager} {script}", *argv),),
    )


def _simple_match(
    root: Path,
    marker: str,
    language: str,
    build_system: str,
    label: str,
    argv: tuple[str, ...],
) -> ProjectMatch | None:
    if not (root / marker).is_file():
        return None
    return ProjectMatch(language, build_system, (marker,), (_step(label, *argv),))


def inspect_project(path: Path | str) -> ProjectInspection:
    """Detect supported build systems in priority order."""

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Project directory does not exist: {root}")

    candidates = (
        _python_match(root),
        _cmake_match(root),
        _maven_match(root),
        _gradle_match(root),
        _node_match(root),
        _simple_match(root, "Cargo.toml", "Rust", "cargo", "Run Cargo tests", ("cargo", "test")),
        _simple_match(root, "go.mod", "Go", "go", "Run Go tests", ("go", "test", "./...")),
    )
    return ProjectInspection(root=root, matches=tuple(item for item in candidates if item is not None))

