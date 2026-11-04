from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autofix.builders.docker import (
    DockerConfig,
    DockerConfigurationError,
    build_docker_step,
    collect_docker_toolchain_identity,
    resolve_docker_image_identity,
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


def test_resolves_tag_to_immutable_image_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    repo_digest = "python@sha256:" + "b" * 64

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[1:3] == ("image", "inspect")
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps([{"Id": image_id, "RepoDigests": [repo_digest]}]),
            "",
        )

    monkeypatch.setattr("autofix.builders.docker.subprocess.run", fake_run)

    identity = resolve_docker_image_identity(DockerConfig("python:3.11-slim"))

    assert identity.reference == "python:3.11-slim"
    assert identity.image_id == image_id
    assert identity.repo_digests == (repo_digest,)


def test_container_toolchains_use_pinned_constrained_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(argv, 0, "go version go1.21.2 linux/amd64\n", "")

    monkeypatch.setattr("autofix.builders.docker.subprocess.run", fake_run)

    identity = collect_docker_toolchain_identity(
        DockerConfig(image_id), (("go", "version"),)
    )

    assert identity[0]["version"] == "go version go1.21.2 linux/amd64"
    argv = calls[0]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--entrypoint") + 1] == "go"
    assert image_id in argv

