"""Load, validate, merge, and create AutoFix YAML configuration files."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when configuration is malformed or internally inconsistent."""

    pass


@dataclass(frozen=True)
class AutoFixConfig:
    """Validated effective configuration after global/project merging."""

    path: Path
    version: int
    command: str | None
    commands: tuple[str, ...]
    build_system: str | None
    docker_image: str | None
    ollama_model: str | None
    offline: bool
    dependencies_bundled: bool


def global_config_path() -> Path:
    """Return the user-level settings path, honoring ``AUTOFIX_HOME``."""

    configured_home = os.environ.get("AUTOFIX_HOME")
    base = Path(configured_home).expanduser() if configured_home else Path.home() / ".autofix"
    return base / "settings.yml"


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Unable to read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration must be a mapping: {path}")
    return raw


def save_global_config(
    *,
    docker_image: str,
    ollama_model: str,
    offline: bool = True,
    dependencies_bundled: bool = True,
) -> Path:
    """Validate and persist defaults inherited by every project."""

    if not docker_image.strip() or not ollama_model.strip():
        raise ConfigError("docker_image and ollama_model must be non-empty")
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "build": {},
        "runtime": {
            "docker_image": docker_image.strip(),
            "ollama_model": ollama_model.strip(),
            "offline": offline,
            "dependencies_bundled": dependencies_bundled,
        },
    }
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    load_config(Path.cwd(), global_only=True)
    return path


def create_config(
    root: Path | str,
    *,
    command: str | None = None,
    commands: tuple[str, ...] = (),
    build_system: str | None = None,
    docker_image: str,
    ollama_model: str,
    offline: bool = True,
    dependencies_bundled: bool = False,
) -> Path:
    """Create a project configuration without overwriting existing settings."""

    project_root = Path(root).expanduser().resolve()
    path = project_root / "autofix.yml"
    if path.exists() or (project_root / ".autofix.yml").exists():
        raise ConfigError("An AutoFix configuration already exists")
    if command is not None and not command.strip():
        raise ConfigError("build.command must be a non-empty string")
    if any(not item.strip() for item in commands):
        raise ConfigError("build.commands must contain only non-empty strings")
    normalized_command = command.strip() if command else None
    normalized_commands = tuple(item.strip() for item in commands)
    if normalized_command and normalized_commands:
        raise ConfigError("build.command and build.commands cannot both be configured")
    if not normalized_command and not normalized_commands:
        raise ConfigError("A build command or command matrix is required")
    build: dict[str, Any] = {
        **({"command": normalized_command} if normalized_command else {}),
        **({"commands": list(normalized_commands)} if normalized_commands else {}),
        **({"system": build_system} if build_system else {}),
    }
    data = {
        "version": 1,
        "build": build,
        "runtime": {
            "docker_image": docker_image,
            "ollama_model": ollama_model,
            "offline": offline,
            "dependencies_bundled": dependencies_bundled,
        },
    }
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # Read the generated file through the normal parser before reporting success.
    load_config(project_root)
    return path


def load_config(root: Path | str, *, global_only: bool = False) -> AutoFixConfig | None:
    """Merge global and project settings into one validated configuration."""

    project_root = Path(root).expanduser().resolve()
    project_paths = (project_root / "autofix.yml", project_root / ".autofix.yml")
    project_path = None if global_only else next(
        (candidate for candidate in project_paths if candidate.is_file()), None
    )
    settings_path = global_config_path()
    global_raw = _read_mapping(settings_path) if settings_path.is_file() else {}
    project_raw = _read_mapping(project_path) if project_path else {}
    if not global_raw and not project_raw:
        return None
    for source, source_path in ((global_raw, settings_path), (project_raw, project_path)):
        if not source:
            continue
        if (
            source.get("version", 1) != 1
            or not isinstance(source.get("build", {}), dict)
            or not isinstance(source.get("runtime", {}), dict)
        ):
            raise ConfigError(f"Unsupported or invalid configuration: {source_path}")
    raw: dict[str, Any] = dict(global_raw)
    global_build = dict(global_raw.get("build", {}))
    project_build = dict(project_raw.get("build", {}))
    if "command" in project_build:
        global_build.pop("commands", None)
    if "commands" in project_build:
        global_build.pop("command", None)
    raw["build"] = {**global_build, **project_build}
    raw["runtime"] = {
        **(
            global_raw.get("runtime", {})
            if isinstance(global_raw.get("runtime", {}), dict)
            else {}
        ),
        **(
            project_raw.get("runtime", {})
            if isinstance(project_raw.get("runtime", {}), dict)
            else {}
        ),
    }
    if "version" in project_raw:
        raw["version"] = project_raw["version"]
    path = project_path or settings_path
    version = raw.get("version", 1)
    build = raw.get("build", {})
    runtime = raw.get("runtime", {})
    if version != 1 or not isinstance(build, dict) or not isinstance(runtime, dict):
        raise ConfigError(f"Unsupported or invalid configuration: {path}")

    def optional_string(mapping: dict[str, Any], key: str) -> str | None:
        """Normalize an optional non-empty scalar from a config section."""

        value = mapping.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"{key} must be a non-empty string")
        return value.strip() if isinstance(value, str) else None

    raw_commands = build.get("commands", [])
    if not isinstance(raw_commands, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_commands
    ):
        raise ConfigError("commands must be a list of non-empty strings")
    if "commands" in build and not raw_commands:
        raise ConfigError("commands must contain at least one command")
    commands = tuple(item.strip() for item in raw_commands)
    command = optional_string(build, "command")
    if command and commands:
        raise ConfigError("build.command and build.commands cannot both be configured")

    offline = runtime.get("offline", False)
    dependencies_bundled = runtime.get("dependencies_bundled", False)
    if not isinstance(offline, bool) or not isinstance(dependencies_bundled, bool):
        raise ConfigError("offline and dependencies_bundled must be booleans")
    return AutoFixConfig(
        path=path,
        version=version,
        command=command,
        commands=commands,
        build_system=optional_string(build, "system"),
        docker_image=optional_string(runtime, "docker_image"),
        ollama_model=optional_string(runtime, "ollama_model"),
        offline=offline,
        dependencies_bundled=dependencies_bundled,
    )

