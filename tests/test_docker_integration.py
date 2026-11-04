from __future__ import annotations

import os

import pytest

from autofix.builders import (
    DockerConfig,
    collect_docker_toolchain_identity,
    resolve_docker_image_identity,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFIX_RUN_DOCKER_TESTS") != "1",
    reason="real Docker integration is enabled only on the Docker CI job",
)


def test_real_image_identity_and_container_toolchain() -> None:
    configured = DockerConfig("autofix-python:3.11")

    image = resolve_docker_image_identity(configured)
    pinned = DockerConfig(
        image.image_id,
        configured.cpus,
        configured.memory,
        configured.pids_limit,
        configured.allow_network,
    )
    tools = collect_docker_toolchain_identity(
        pinned, (("python", "--version"),)
    )

    assert image.reference == configured.image
    assert image.image_id.startswith("sha256:")
    assert tools == [
        {
            "name": "python",
            "command": ["python", "--version"],
            "available": True,
            "version": tools[0]["version"],
        }
    ]
    assert str(tools[0]["version"]).startswith("Python 3.11")
