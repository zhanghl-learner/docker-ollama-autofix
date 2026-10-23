from __future__ import annotations

from pathlib import Path

import pytest

from autofix.builders.docker import (
    DockerConfig,
    DockerConfigurationError,
    build_docker_step,
)
from autofix.models import BuildStep


def test_docker_command_is_constrained(tmp_path: Path) -> None:
    step = BuildStep("test", ("python", "-m", "pytest"))

    docker_step = build_docker_step(tmp_path, step, DockerConfig("python:3.11-slim"))

    argv = docker_step.argv
    assert Path(argv[0]).name.lower() in {"docker", "docker.exe"}
    assert argv[1:3] == ("run", "--rm")
    assert ("--pull", "never") == argv[3:5]
    assert "--read-only" in argv
    assert "--cap-drop" in argv
    assert "no-new-privileges" in argv
    assert ("--network", "none") == argv[argv.index("--network") : argv.index("--network") + 2]
    assert argv[-4:] == ("python:3.11-slim", "python", "-m", "pytest")
    mount = argv[argv.index("--mount") + 1]
    assert str(tmp_path.resolve()) in mount
    assert "target=/workspace" in mount


def test_network_can_only_be_enabled_explicitly(tmp_path: Path) -> None:
    config = DockerConfig("python:3.11-slim", allow_network=True)
    docker_step = build_docker_step(tmp_path, BuildStep("test", ("python", "-V")), config)

    assert "--network" not in docker_step.argv


@pytest.mark.parametrize("image", ["--privileged", "bad image", "", "$(evil)"])
def test_rejects_unsafe_image_names(image: str) -> None:
    with pytest.raises(DockerConfigurationError):
        DockerConfig(image)


def test_validates_resource_limits() -> None:
    with pytest.raises(DockerConfigurationError):
        DockerConfig("python:3.11", memory="unlimited")
    with pytest.raises(DockerConfigurationError):
        DockerConfig("python:3.11", cpus=0)
    with pytest.raises(DockerConfigurationError):
        DockerConfig("python:3.11", pids_limit=2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpus", float("nan")),
        ("cpus", float("inf")),
        ("cpus", True),
        ("pids_limit", True),
        ("allow_network", 1),
    ],
)
def test_rejects_invalid_resource_types_and_non_finite_values(
    field: str, value: object
) -> None:
    with pytest.raises(DockerConfigurationError):
        DockerConfig("python:3.11", **{field: value})

