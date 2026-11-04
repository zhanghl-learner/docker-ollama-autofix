"""Run build matrices in disposable, resource-constrained Docker containers."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from autofix.builders.runner import BuildResult, run_build
from autofix.models import BuildStep, ProjectMatch
from autofix.runtime.tools import find_executable


IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
MEMORY_PATTERN = re.compile(r"^[1-9][0-9]*(?:[bkmgBKMG])?$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_DIGEST_PATTERN = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
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
    """Raised when Docker is unavailable or a container policy is unsafe."""

    pass


@dataclass(frozen=True)
class DockerImageIdentity:
    """Immutable local identity for the exact image used by an evaluation."""

    reference: str
    image_id: str
    repo_digests: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return stable, path-free image evidence for JSON reports."""

        return {
            "reference": self.reference,
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
        }


@dataclass(frozen=True)
class DockerConfig:
    """Validated container image, resource limits, and network policy."""

    image: str
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 256
    allow_network: bool = False

    def __post_init__(self) -> None:
        """Reject malformed values before they can reach ``docker run``."""

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
    """Return the conventional image for a detected build system."""

    return DEFAULT_IMAGES.get(match.build_system) if match else None


def resolve_docker_image_identity(
    config: DockerConfig, *, timeout_seconds: float = 10
) -> DockerImageIdentity:
    """Resolve one configured tag to an immutable local image ID without pulling."""

    docker = find_executable("docker") or "docker"
    try:
        completed = subprocess.run(
            (docker, "image", "inspect", config.image),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerConfigurationError(
            f"Unable to inspect Docker image '{config.image}': {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[:500]
        raise DockerConfigurationError(
            f"Unable to inspect Docker image '{config.image}': {detail or 'docker inspect failed'}"
        )
    try:
        payload = json.loads(completed.stdout)
        inspected = payload[0]
        image_id = inspected["Id"]
        raw_repo_digests = inspected.get("RepoDigests") or []
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise DockerConfigurationError(
            f"Docker returned invalid identity data for image '{config.image}'"
        ) from exc
    if not isinstance(image_id, str) or not IMAGE_ID_PATTERN.fullmatch(image_id):
        raise DockerConfigurationError(
            f"Docker image '{config.image}' has an invalid content ID"
        )
    if not isinstance(raw_repo_digests, list) or any(
        not isinstance(item, str)
        or len(item) > 500
        or not REPO_DIGEST_PATTERN.fullmatch(item)
        for item in raw_repo_digests
    ):
        raise DockerConfigurationError(
            f"Docker image '{config.image}' has invalid repository digests"
        )
    return DockerImageIdentity(
        reference=config.image,
        image_id=image_id,
        repo_digests=tuple(sorted(set(raw_repo_digests))),
    )


def collect_docker_toolchain_identity(
    config: DockerConfig,
    commands: tuple[tuple[str, ...], ...],
    *,
    timeout_seconds: float = 10,
) -> list[dict[str, object]]:
    """Run fixed version commands inside one already-pinned constrained image."""

    if not IMAGE_ID_PATTERN.fullmatch(config.image):
        raise DockerConfigurationError(
            "Container toolchain probes require an immutable sha256 image ID"
        )
    docker = find_executable("docker") or "docker"
    identity: list[dict[str, object]] = []
    for command in commands:
        argv = [
            docker,
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
            "/tmp:rw,noexec,nosuid,size=64m",
            "--network",
            "none",
            "--workdir",
            "/tmp",
            "--env",
            "HOME=/tmp/home",
            "--entrypoint",
            command[0],
            config.image,
            *command[1:],
        ]
        version = None
        available = False
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
            output = completed.stdout.strip() or completed.stderr.strip()
            available = completed.returncode == 0 and bool(output)
            version = output.splitlines()[0][:500] if available else None
        except (OSError, subprocess.SubprocessError):
            available = False
        identity.append(
            {
                "name": command[0],
                "command": list(command),
                "available": available,
                "version": version,
            }
        )
    return identity


def build_docker_step(source: Path, step: BuildStep, config: DockerConfig) -> BuildStep:
    """Wrap one project command in a hardened ``docker run`` invocation."""

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

