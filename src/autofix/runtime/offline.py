"""Check whether a project can be repaired without network access."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from autofix.config import AutoFixConfig, load_config
from autofix.detectors import inspect_project
from autofix.runtime.tools import find_executable


@dataclass(frozen=True)
class ReadinessCheck:
    """One named offline prerequisite and its actionable status."""

    name: str
    ready: bool
    required: bool
    details: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the check for JSON CLI output."""

        return asdict(self)


@dataclass(frozen=True)
class OfflineReadinessReport:
    """Aggregate build, Docker, dependency, and Ollama readiness checks."""

    root: Path
    config_path: Path | None
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        """Return true only when every prerequisite is ready."""

        return all(check.ready for check in self.checks if check.required)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report and its aggregate readiness flag."""

        return {
            "root": str(self.root),
            "config_path": str(self.config_path) if self.config_path else None,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }


def _docker_check(image: str | None) -> tuple[ReadinessCheck, ReadinessCheck]:
    docker = find_executable("docker")
    if docker is None:
        missing = ReadinessCheck("docker-engine", False, True, "docker executable not found")
        image_check = ReadinessCheck("docker-image", False, True, image or "image not configured")
        return missing, image_check
    try:
        info = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        engine_ready = info.returncode == 0
        engine_details = (info.stdout or info.stderr).strip() or "Docker Engine unavailable"
    except (OSError, subprocess.SubprocessError) as exc:
        engine_ready, engine_details = False, str(exc)
    engine = ReadinessCheck("docker-engine", engine_ready, True, engine_details)

    if not image:
        return engine, ReadinessCheck("docker-image", False, True, "not configured")
    try:
        inspected = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        image_ready = inspected.returncode == 0
        details = image if image_ready else (inspected.stderr.strip() or f"missing: {image}")
    except (OSError, subprocess.SubprocessError) as exc:
        image_ready, details = False, str(exc)
    return engine, ReadinessCheck("docker-image", image_ready, True, details)


def _ollama_check(base_url: str, model: str | None) -> tuple[ReadinessCheck, ReadinessCheck]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        models = [item.get("name") for item in response.json().get("models", [])]
        service = ReadinessCheck("ollama-api", True, True, base_url)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        service = ReadinessCheck("ollama-api", False, True, str(exc))
        return service, ReadinessCheck("ollama-model", False, True, model or "model not configured")
    ready = bool(model and model in models)
    details = model if ready else f"missing: {model}" if model else "not configured"
    return service, ReadinessCheck("ollama-model", ready, True, details)


def check_offline_readiness(
    root: Path | str,
    *,
    image: str | None = None,
    model: str | None = None,
    build_system: str | None = None,
    ollama_url: str = "http://127.0.0.1:11434",
) -> OfflineReadinessReport:
    """Evaluate all prerequisites required for offline project repair."""

    project_root = Path(root).expanduser().resolve()
    config: AutoFixConfig | None = load_config(project_root)
    selected_image = image or (config.docker_image if config else None)
    selected_model = model or (config.ollama_model if config else None)
    inspection = inspect_project(project_root)
    selected_system = build_system or (config.build_system if config else None)
    inspection = inspection.select(selected_system)

    checks: list[ReadinessCheck] = [
        ReadinessCheck(
            "build-plan",
            inspection.selected is not None
            or bool(config and (config.command or config.commands)),
            True,
            (
                f"{len(config.commands)} configured steps"
                if config and config.commands
                else inspection.selected.build_system
                if inspection.selected
                else config.command
                if config and config.command
                else "not detected"
            ),
        ),
        ReadinessCheck(
            "offline-policy",
            bool(config and config.offline),
            True,
            "network disabled" if config and config.offline else "runtime.offline is not true",
        ),
        ReadinessCheck(
            "dependencies",
            bool(config and config.dependencies_bundled),
            True,
            "declared bundled" if config and config.dependencies_bundled else "offline dependencies not confirmed",
        ),
    ]
    checks.extend(_docker_check(selected_image))
    checks.extend(_ollama_check(ollama_url, selected_model))
    return OfflineReadinessReport(
        project_root,
        config.path if config else None,
        tuple(checks),
    )

