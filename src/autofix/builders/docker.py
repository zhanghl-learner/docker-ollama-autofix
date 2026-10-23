from __future__ import annotations

import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from autofix.builders.runner import BuildResult, run_build
from autofix.models import BuildStep, ProjectMatch
from autofix.runtime.tools import find_executable


IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
MEMORY_PATTERN = re.compile(r"^[1-9][0-9]*(?:[bkmgBKMG])?$")
COPY_IGNORED_PARTS = {
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

DEFAULT_IMAGES = {
    "python": "python:3.11-slim",
    "cmake": "gcc:latest",
    "maven": "maven:3-eclipse-temurin-21",
    "gradle": "gradle:8-jdk21",
    "npm": "node:lts",
    "pnpm": "node:lts",
    "yarn": "node:lts",
    "cargo": "rust:latest",
    "go": "golang:latest",
}


class DockerConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DockerConfig:
    image: str
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 256
    allow_network: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not IMAGE_PATTERN.fullmatch(self.image):
            raise DockerConfigurationError(f"Invalid Docker image reference: {self.image}")
        if (
            type(self.cpus) not in {int, float}
            or not math.isfinite(self.cpus)
            or self.cpus <= 0
            or self.cpus > 64
        ):
            raise DockerConfigurationError("Docker CPU limit must be between 0 and 64")
        if not isinstance(self.memory, str) or not MEMORY_PATTERN.fullmatch(self.memory):
            raise DockerConfigurationError("Docker memory must look like 512m or 2g")
        if (
            type(self.pids_limit) is not int
            or self.pids_limit < 16
            or self.pids_limit > 4096
        ):
            raise DockerConfigurationError("Docker PID limit must be between 16 and 4096")
        if type(self.allow_network) is not bool:
            raise DockerConfigurationError("Docker network policy must be a boolean")


def default_image(match: ProjectMatch | None) -> str | None:
    return DEFAULT_IMAGES.get(match.build_system) if match else None


def build_docker_step(source: Path, step: BuildStep, config: DockerConfig) -> BuildStep:
    source = source.resolve()
    if "," in str(source):
        raise DockerConfigurationError("Docker mount source paths containing commas are unsupported")
    mount = f"type=bind,source={source},target=/workspace"
    argv = [
        find_executable("docker") or "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--init",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        f"{config.cpus:g}",
        "--memory",
        config.memory,
        "--pids-limit",
        str(config.pids_limit),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",
        "--mount",
        mount,
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "XDG_CACHE_HOME=/tmp/cache",
        "--env",
        "PYTHONPYCACHEPREFIX=/tmp/pycache",
    ]
    if not config.allow_network:
        argv.extend(("--network", "none"))
    argv.append(config.image)
    argv.extend(step.argv)
    return BuildStep(label=f"Docker: {step.label}", argv=tuple(argv))


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    ignored = {name for name in names if name in COPY_IGNORED_PARTS}
    ignored.update(name for name in names if (base / name).is_symlink())
    return ignored


def run_build_docker(
    root: Path | str,
    steps: tuple[BuildStep, ...],
    *,
    config: DockerConfig,
    timeout_seconds: float = 300,
    save_report: bool = True,
) -> BuildResult:
    """Run build commands in a constrained container against a disposable copy."""

    project_root = Path(root).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="autofix-docker-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(project_root, workspace, ignore=_copy_ignore)
        docker_steps = tuple(build_docker_step(workspace, step, config) for step in steps)
        return run_build(
            project_root,
            docker_steps,
            timeout_seconds=timeout_seconds,
            save_report=save_report,
        )

