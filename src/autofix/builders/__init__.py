"""Build execution primitives for local processes and Docker containers."""

from autofix.builders.docker import (
    DockerConfig,
    DockerConfigurationError,
    DockerImageIdentity,
    build_docker_step,
    collect_docker_toolchain_identity,
    default_image,
    resolve_docker_image_identity,
    run_build_docker,
)
from autofix.builders.runner import BuildResult, StepResult, parse_command, run_build

__all__ = [
    "BuildResult",
    "DockerConfig",
    "DockerConfigurationError",
    "DockerImageIdentity",
    "StepResult",
    "build_docker_step",
    "collect_docker_toolchain_identity",
    "default_image",
    "parse_command",
    "run_build",
    "run_build_docker",
    "resolve_docker_image_identity",
]

