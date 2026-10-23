from __future__ import annotations

import json
import os
from pathlib import Path

from autofix.detectors import inspect_project


def test_detects_python_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    inspection = inspect_project(tmp_path)

    assert inspection.selected is not None
    assert inspection.selected.language == "Python"
    assert inspection.selected.steps[0].argv == ("python", "-m", "pytest")


def test_detects_root_python_test_file(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    inspection = inspect_project(tmp_path)

    assert inspection.selected is not None
    assert inspection.selected.steps[0].argv == ("python", "-m", "pytest")


def test_detects_single_runnable_python_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "nnModule.py").write_text(
        "import math\n\nvalue = math.sqrt(4)\nprint(value)\n", encoding="utf-8"
    )

    inspection = inspect_project(tmp_path)

    assert inspection.selected is not None
    assert inspection.selected.steps[0].argv == ("python", "nnModule.py")


def test_keeps_compileall_for_passive_python_library(tmp_path: Path) -> None:
    (tmp_path / "library.py").write_text(
        "VALUE = 1\n\ndef calculate():\n    return VALUE\n", encoding="utf-8"
    )

    inspection = inspect_project(tmp_path)

    assert inspection.selected is not None
    assert inspection.selected.steps[0].argv == (
        "python",
        "-m",
        "compileall",
        "-q",
        ".",
    )


def test_detects_node_build_when_test_is_absent(tmp_path: Path) -> None:
    package = {"scripts": {"build": "tsc"}}
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")

    inspection = inspect_project(tmp_path)

    assert inspection.selected is not None
    assert inspection.selected.build_system == "npm"
    executable = "npm.cmd" if os.name == "nt" else "npm"
    assert inspection.selected.steps[0].argv == (executable, "run", "build")


def test_reports_no_match_for_empty_directory(tmp_path: Path) -> None:
    assert inspect_project(tmp_path).selected is None


def test_explicitly_selects_detected_build_system(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")

    inspection = inspect_project(tmp_path).select("cargo")

    assert inspection.selected is not None
    assert inspection.selected.build_system == "cargo"
    assert inspection.selected.steps[0].argv == ("cargo", "test")


def test_rejects_build_system_that_was_not_detected(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    try:
        inspect_project(tmp_path).select("cargo")
    except ValueError as exc:
        assert "detected systems: python" in str(exc)
    else:
        raise AssertionError("missing build system should be rejected")

