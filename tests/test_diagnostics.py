from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.agent.diagnostics import environment_failure
from autofix.builders import BuildResult, StepResult


def _failure(stderr: str) -> BuildResult:
    return BuildResult(
        Path("."),
        (StepResult("run", ("python", "app.py"), 1, "", stderr, 0.1),),
        "2026-10-21T00:00:00+00:00",
    )


def test_classifies_missing_python_package_as_environment_failure() -> None:
    message = environment_failure(
        _failure("ModuleNotFoundError: No module named 'torch'\n")
    )

    assert message is not None
    assert "'torch'" in message
    assert "Code repair was not attempted" in message


def test_name_error_remains_eligible_for_code_repair() -> None:
    message = environment_failure(_failure("NameError: name 'torch' is not defined\n"))

    assert message is None


def test_classifies_missing_build_executable_as_environment_failure() -> None:
    build = BuildResult(
        Path("."),
        (
            StepResult(
                "Run Cargo tests",
                ("cargo", "test"),
                127,
                "",
                "The system cannot find the file specified",
                0.1,
            ),
        ),
        "2026-10-23T00:00:00+00:00",
    )

    message = environment_failure(build)

    assert message is not None
    assert "'cargo'" in message
    assert "Code repair was not attempted" in message


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("sh: 1: tsc: not found", "nested build tool 'tsc'"),
        (
            "'eslint' is not recognized as an internal or external command",
            "nested build tool 'eslint'",
        ),
        ("No CMAKE_CXX_COMPILER could be found.", "CMake tool 'C++ compiler'"),
        (
            'CMake was unable to find a build program corresponding to "Ninja"',
            "CMake build tool 'Ninja'",
        ),
        ("cgo: C compiler \"gcc\" not found", "Go/cgo tool 'gcc'"),
        ("error: linker `cc` not found", "Rust linker tool 'cc'"),
        ("JAVA_HOME is not defined correctly", "JAVA_HOME"),
        ("No compiler is provided in this environment", "Java compiler"),
        ("error: release version 21 not supported", "Java release 21"),
        (
            "attempting to make an HTTP request, but --offline was specified",
            "Rust dependency",
        ),
        ("Cannot connect to the Docker daemon", "Docker daemon"),
        ("docker: Error response from daemon: No such image: node:lts", "node:lts"),
    ],
)
def test_classifies_multilanguage_environment_failures(
    stderr: str, expected: str
) -> None:
    message = environment_failure(_failure(stderr))

    assert message is not None
    assert expected in message
    assert "Code repair was not attempted" in message


def test_classifies_declared_missing_node_dependency(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "4.17.21"}}), encoding="utf-8"
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "run",
                ("node", "index.js"),
                1,
                "",
                "Error: Cannot find module 'lodash/fp'",
                0.1,
            ),
        ),
        "2026-10-23T00:00:00+00:00",
    )

    message = environment_failure(build)

    assert message is not None
    assert "Node.js dependency 'lodash'" in message


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: Cannot find module './local-helper'",
        "Error: Cannot find module 'possibly-misspelled-package'",
        "main.go:7:2: undefined: calculate",
        "error[E0425]: cannot find value `answer` in this scope",
        "Main.java:4: error: cannot find symbol",
        "src/main.cpp:8:10: error: expected ';' before '}' token",
    ],
)
def test_multilanguage_code_failures_remain_repairable(stderr: str) -> None:
    assert environment_failure(_failure(stderr)) is None


def test_does_not_classify_warning_from_successful_earlier_step() -> None:
    build = BuildResult(
        Path("."),
        (
            StepResult(
                "prepare",
                ("python", "prepare.py"),
                0,
                "old warning: No CMAKE_CXX_COMPILER could be found",
                "",
                0.1,
            ),
            StepResult(
                "test",
                ("python", "test.py"),
                1,
                "",
                "AssertionError: expected 5, got -1",
                0.1,
            ),
        ),
        "2026-10-23T00:00:00+00:00",
    )

    assert environment_failure(build) is None
