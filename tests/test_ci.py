from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.ci import CIExitCode, CI_SCHEMA, CI_SCHEMA_VERSION
from autofix.cli import app


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, program: str = "print('ok')\n") -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "AutoFix CI Tests")
    _git(root, "config", "user.email", "autofix-ci@example.invalid")
    (root / "check.py").write_text(program, encoding="utf-8")
    _git(root, "add", "check.py")
    _git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        "baseline",
    )
    return root


def _invoke(root: Path, *extra: str):
    return runner.invoke(
        app,
        ["ci-check", str(root), "--command", "python check.py", *extra],
    )


def test_ci_check_passes_with_stable_json_envelope(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    result = _invoke(root)

    assert result.exit_code == CIExitCode.PASSED
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema",
        "version",
        "created_at",
        "status",
        "exit_code",
        "root",
        "runner",
        "repository",
        "build",
        "error",
    }
    assert payload["schema"] == CI_SCHEMA
    assert payload["version"] == CI_SCHEMA_VERSION
    assert payload["status"] == "passed"
    assert payload["exit_code"] == 0
    assert payload["runner"] == "local"
    assert payload["repository"]["clean"] is True
    assert payload["build"]["succeeded"] is True
    assert payload["error"] is None


def test_committed_ci_schema_matches_runtime_envelope() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-ci-result-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema"]["const"] == CI_SCHEMA
    assert schema["properties"]["version"]["const"] == CI_SCHEMA_VERSION
    assert set(schema["required"]) == {
        "schema",
        "version",
        "created_at",
        "status",
        "exit_code",
        "root",
        "runner",
        "repository",
        "build",
        "error",
    }
    assert set(schema["properties"]["exit_code"]["enum"]) == {
        int(code) for code in CIExitCode
    }


def test_ci_check_maps_test_failure_to_reserved_exit_code(tmp_path: Path) -> None:
    root = _repository(tmp_path, "raise SystemExit(3)\n")

    result = _invoke(root)

    assert result.exit_code == CIExitCode.VERIFICATION_FAILED
    payload = json.loads(result.stdout)
    assert payload["status"] == "verification_failed"
    assert payload["exit_code"] == 10
    assert payload["build"]["exit_code"] == 3


def test_ci_check_classifies_missing_tool_as_environment_error(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    result = runner.invoke(
        app,
        [
            "ci-check",
            str(root),
            "--command",
            "definitely-missing-autofix-ci-tool",
        ],
    )

    assert result.exit_code == CIExitCode.ENVIRONMENT_ERROR
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment_error"
    assert "was not found" in payload["error"]
    assert payload["build"]["steps"][0]["exit_code"] == 127


def test_ci_check_rejects_dirty_repository_before_build(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "local.txt").write_text("do not touch\n", encoding="utf-8")

    result = _invoke(root)

    assert result.exit_code == CIExitCode.POLICY_REJECTED
    payload = json.loads(result.stdout)
    assert payload["status"] == "policy_rejected"
    assert payload["build"] is None
    assert payload["repository"]["clean"] is False


def test_ci_check_allows_explicit_unversioned_project(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    result = _invoke(tmp_path, "--allow-unversioned")

    assert result.exit_code == CIExitCode.PASSED
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["repository"]["is_repository"] is False


def test_ci_check_rejects_unversioned_project_by_default(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    result = _invoke(tmp_path)

    assert result.exit_code == CIExitCode.POLICY_REJECTED
    payload = json.loads(result.stdout)
    assert payload["status"] == "policy_rejected"
    assert payload["build"] is None
    assert "requires a Git repository" in payload["error"]


def test_ci_check_reports_configuration_error_without_build(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    result = runner.invoke(
        app,
        ["ci-check", str(root), "--command", "python check.py", "--step", "python -V"],
    )

    assert result.exit_code == CIExitCode.CONFIGURATION_ERROR
    payload = json.loads(result.stdout)
    assert payload["status"] == "configuration_error"
    assert payload["build"] is None
    assert "cannot be used together" in payload["error"]


def test_ci_check_atomically_writes_same_result_to_output(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    output = tmp_path / "artifacts" / "ci-result.json"

    result = _invoke(root, "--output", str(output))

    assert result.exit_code == CIExitCode.PASSED
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(result.stdout)


def test_ci_check_maps_unexpected_exception_to_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "autofix.cli.inspect_git_workspace",
        lambda _: (_ for _ in ()).throw(RuntimeError("unexpected test failure")),
    )

    result = _invoke(tmp_path)

    assert result.exit_code == CIExitCode.INTERNAL_ERROR
    payload = json.loads(result.stdout)
    assert payload["status"] == "internal_error"
    assert payload["repository"] is None
    assert payload["build"] is None
    assert payload["error"] == "RuntimeError: unexpected test failure"
