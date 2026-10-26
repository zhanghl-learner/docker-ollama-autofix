"""Classify high-confidence environment failures before model invocation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from autofix.builders import BuildResult


class RepairEnvironmentError(RuntimeError):
    """Raised when source edits cannot repair the selected runtime."""

    pass


_MISSING_PYTHON_MODULE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"](?P<name>[^'\"]+)['\"]"
)
_DLL_IMPORT_FAILURE = re.compile(
    r"ImportError:\s+DLL load failed(?: while importing (?P<name>[A-Za-z0-9_.-]+))?"
)
_MISSING_NODE_DEPENDENCY = re.compile(
    r"Cannot find (?:module|package)\s+['\"](?P<name>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_MISSING_NESTED_POSIX_TOOL = re.compile(
    r"(?:/bin/)?sh:\s*\d+:\s*(?P<name>[A-Za-z0-9_.+-]+):\s*not found",
    re.IGNORECASE,
)
_MISSING_NESTED_WINDOWS_TOOL = re.compile(
    r"['\"]?(?P<name>[A-Za-z0-9_.+-]+)['\"]?\s+is not recognized as an "
    r"internal or external command",
    re.IGNORECASE,
)
_MISSING_CMAKE_COMPILER = re.compile(
    r"No CMAKE_(?P<language>C|CXX)_COMPILER could be found",
    re.IGNORECASE,
)
_MISSING_CMAKE_BUILD_TOOL = re.compile(
    r'CMake was unable to find a build program corresponding to "(?P<name>[^"]+)"',
    re.IGNORECASE,
)
_MISSING_GO_C_COMPILER = re.compile(
    r"cgo:\s*C compiler\s+['\"]?(?P<name>[A-Za-z0-9_.+-]+)['\"]?\s+not found",
    re.IGNORECASE,
)
_MISSING_RUST_LINKER = re.compile(
    r"error:\s*linker\s+[`'\"](?P<name>[^`'\"]+)[`'\"]\s+not found",
    re.IGNORECASE,
)
_JAVA_HOME_FAILURE = re.compile(
    r"JAVA_HOME\s+(?:is not set|is not defined|is set to an invalid directory)",
    re.IGNORECASE,
)
_MISSING_JAVA_COMPILER = re.compile(
    r"No compiler is provided in this environment",
    re.IGNORECASE,
)
_UNSUPPORTED_JAVA_RELEASE = re.compile(
    r"(?:invalid target release\s*:\s*|release version\s+)(?P<version>\d+)"
    r"(?:\s+not supported)?",
    re.IGNORECASE,
)
_CARGO_OFFLINE_MISS = re.compile(
    r"attempting to make an HTTP request, but --offline was specified",
    re.IGNORECASE,
)
_DOCKER_DAEMON_FAILURE = re.compile(
    r"(?:Cannot connect to the Docker daemon|error during connect:.*docker daemon)",
    re.IGNORECASE,
)
_MISSING_DOCKER_IMAGE = re.compile(
    r"(?:No such image:\s*|Unable to find image\s+['\"])(?P<name>[A-Za-z0-9._/:@-]+)",
    re.IGNORECASE,
)


def _missing_tool_message(tool: str, ecosystem: str) -> str:
    return (
        f"Missing {ecosystem} tool '{tool}' in the selected runtime. "
        "Code repair was not attempted. Install the required tool or use a Docker "
        "image that contains it."
    )


def _node_package_name(request: str) -> str | None:
    if request.startswith((".", "/", "\\", "node:")) or re.match(
        r"^[A-Za-z]:[\\/]", request
    ):
        return None
    parts = request.split("/")
    if request.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def _declared_node_dependency(root: Path, request: str) -> str | None:
    package = _node_package_name(request)
    if package is None:
        return None
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        dependencies = data.get(key)
        if isinstance(dependencies, dict) and package in dependencies:
            return package
    return None


def environment_failure(build: BuildResult) -> str | None:
    """Return an actionable diagnosis only for high-confidence runtime failures.

    Only failed steps are inspected. Ambiguous compiler and source errors return
    ``None`` so they remain eligible for the ordinary repair loop.
    """

    missing_executable = next((step for step in build.steps if step.exit_code == 127), None)
    if missing_executable:
        executable = missing_executable.argv[0] if missing_executable.argv else "unknown"
        return (
            f"Build executable '{executable}' was not found in the selected runtime. "
            "Code repair was not attempted. Install the required toolchain or select "
            "a Docker image that contains it."
        )
    failed_steps = tuple(step for step in build.steps if not step.succeeded)
    output = "\n".join(f"{step.stdout}\n{step.stderr}" for step in failed_steps)
    missing = _MISSING_PYTHON_MODULE.search(output)
    if missing:
        name = missing.group("name")
        return (
            f"Missing Python dependency '{name}' in the selected runtime. "
            "Code repair was not attempted. Install the dependency locally or add it "
            "to the Docker image used by this project."
        )
    dll_failure = _DLL_IMPORT_FAILURE.search(output)
    if dll_failure:
        name = dll_failure.group("name") or "unknown module"
        return (
            f"Native dependency loading failed for '{name}' in the selected runtime. "
            "Code repair was not attempted. Repair the Python/native package installation "
            "or use a compatible Docker image."
        )
    node_dependency = _MISSING_NODE_DEPENDENCY.search(output)
    if node_dependency:
        name = _declared_node_dependency(build.root, node_dependency.group("name"))
        if name:
            return (
                f"Missing Node.js dependency '{name}' in the selected runtime. "
                "Code repair was not attempted. Restore the lockfile dependencies or "
                "use a Docker image that already contains them."
            )
    nested_tool = _MISSING_NESTED_POSIX_TOOL.search(output)
    if nested_tool is None:
        nested_tool = _MISSING_NESTED_WINDOWS_TOOL.search(output)
    if nested_tool:
        return _missing_tool_message(nested_tool.group("name"), "nested build")
    cmake_compiler = _MISSING_CMAKE_COMPILER.search(output)
    if cmake_compiler:
        language = "C++" if cmake_compiler.group("language").upper() == "CXX" else "C"
        return _missing_tool_message(f"{language} compiler", "CMake")
    cmake_tool = _MISSING_CMAKE_BUILD_TOOL.search(output)
    if cmake_tool:
        return _missing_tool_message(cmake_tool.group("name"), "CMake build")
    go_compiler = _MISSING_GO_C_COMPILER.search(output)
    if go_compiler:
        return _missing_tool_message(go_compiler.group("name"), "Go/cgo")
    rust_linker = _MISSING_RUST_LINKER.search(output)
    if rust_linker:
        return _missing_tool_message(rust_linker.group("name"), "Rust linker")
    if _JAVA_HOME_FAILURE.search(output):
        return (
            "JAVA_HOME is missing or invalid in the selected runtime. Code repair was "
            "not attempted. Configure a compatible JDK or use a Docker image that contains it."
        )
    if _MISSING_JAVA_COMPILER.search(output):
        return (
            "A Java compiler is not available in the selected runtime. Code repair was "
            "not attempted. Use a JDK instead of a JRE or select a JDK Docker image."
        )
    java_release = _UNSUPPORTED_JAVA_RELEASE.search(output)
    if java_release:
        return (
            f"Java release {java_release.group('version')} is unsupported by the selected "
            "JDK. Code repair was not attempted. Select a compatible JDK locally or in Docker."
        )
    if _CARGO_OFFLINE_MISS.search(output):
        return (
            "A Rust dependency is not cached for Cargo offline mode. Code repair was not "
            "attempted. Vendor or prefetch dependencies before running offline repair."
        )
    if _DOCKER_DAEMON_FAILURE.search(output):
        return (
            "The Docker daemon is unavailable. Code repair was not attempted. Start Docker "
            "Desktop or repair the daemon connection, then retry."
        )
    docker_image = _MISSING_DOCKER_IMAGE.search(output)
    if docker_image:
        return (
            f"Docker image '{docker_image.group('name')}' is not available locally. "
            "Code repair was not attempted. Build, load, or pull the configured image first."
        )
    return None
