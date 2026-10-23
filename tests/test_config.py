from __future__ import annotations

from pathlib import Path

import pytest

from autofix.config import ConfigError, create_config, load_config, save_global_config


def test_loads_runtime_manifest(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build:
  system: python
  command: python -m pytest
runtime:
  docker_image: demo:latest
  ollama_model: code-model
  offline: true
  dependencies_bundled: true
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config is not None
    assert config.build_system == "python"
    assert config.docker_image == "demo:latest"
    assert config.ollama_model == "code-model"
    assert config.offline


def test_rejects_invalid_manifest(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text("version: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_create_config_round_trip(tmp_path: Path) -> None:
    path = create_config(
        tmp_path,
        command="python -m pytest",
        build_system="python",
        docker_image="autofix-python:3.11",
        ollama_model="qwen3:0.6b",
        offline=True,
        dependencies_bundled=True,
    )

    config = load_config(tmp_path)

    assert path == tmp_path / "autofix.yml"
    assert config is not None
    assert config.command == "python -m pytest"
    assert config.build_system == "python"
    assert config.docker_image == "autofix-python:3.11"


def test_create_config_refuses_existing_file(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="already exists"):
        create_config(
            tmp_path,
            command="python -m pytest",
            docker_image="demo:1",
            ollama_model="demo:1",
        )


def test_new_project_inherits_user_defaults(tmp_path: Path) -> None:
    save_global_config(
        docker_image="autofix-python:3.11",
        ollama_model="qwen3:0.6b",
        offline=True,
        dependencies_bundled=True,
    )
    project = tmp_path / "new-project"
    project.mkdir()

    config = load_config(project)

    assert config is not None
    assert config.command is None
    assert config.docker_image == "autofix-python:3.11"
    assert config.ollama_model == "qwen3:0.6b"
    assert config.offline is True
    assert config.dependencies_bundled is True


def test_project_config_overrides_only_declared_user_defaults(tmp_path: Path) -> None:
    save_global_config(
        docker_image="global-image:1",
        ollama_model="global-model:1",
        offline=True,
        dependencies_bundled=True,
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python app.py}\nruntime: {ollama_model: project-model:1}\n",
        encoding="utf-8",
    )

    config = load_config(project)

    assert config is not None
    assert config.path == project / "autofix.yml"
    assert config.command == "python app.py"
    assert config.ollama_model == "project-model:1"
    assert config.docker_image == "global-image:1"
    assert config.offline is True


def test_project_can_select_build_system_without_custom_command(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {system: cargo}\nruntime: {}\n", encoding="utf-8"
    )

    config = load_config(tmp_path)

    assert config is not None
    assert config.build_system == "cargo"
    assert config.command is None


def test_loads_ordered_build_command_matrix(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build:
  commands:
    - python -m compileall -q .
    - python -m pytest
runtime: {}
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config is not None
    assert config.command is None
    assert config.commands == (
        "python -m compileall -q .",
        "python -m pytest",
    )


def test_rejects_command_and_command_matrix_together(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build:
  command: python -m pytest
  commands: [python -m compileall -q .]
runtime: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="cannot both"):
        load_config(tmp_path)


def test_rejects_empty_build_command_matrix(tmp_path: Path) -> None:
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {commands: []}\nruntime: {}\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="at least one"):
        load_config(tmp_path)


def test_project_command_replaces_global_command_matrix(tmp_path: Path) -> None:
    settings = tmp_path / "settings.yml"
    settings.write_text(
        "version: 1\nbuild: {commands: [python -m pytest]}\nruntime: {}\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python app.py}\nruntime: {}\n",
        encoding="utf-8",
    )

    import os

    previous = os.environ.get("AUTOFIX_HOME")
    os.environ["AUTOFIX_HOME"] = str(tmp_path)
    try:
        config = load_config(project)
    finally:
        if previous is None:
            os.environ.pop("AUTOFIX_HOME", None)
        else:
            os.environ["AUTOFIX_HOME"] = previous

    assert config is not None
    assert config.command == "python app.py"
    assert config.commands == ()

