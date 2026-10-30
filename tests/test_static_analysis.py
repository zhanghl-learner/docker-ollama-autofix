from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.builders import BuildResult, StepResult
from autofix.cli import app
from autofix.static_analysis import inspect_static_analysis


runner = CliRunner()


def test_detects_declared_available_ruff_and_mypy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "sample"
version = "0.1.0"

[project.optional-dependencies]
dev = ["ruff>=0.6", "mypy>=1.0"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name in {"ruff", "mypy"} else None,
    )

    inspection = inspect_static_analysis(tmp_path)

    assert [item.name for item in inspection.analyzers] == ["ruff", "mypy"]
    assert all(item.available for item in inspection.analyzers)
    assert inspection.available_steps[0].argv == ("ruff", "check", ".")
    assert inspection.available_steps[1].argv == ("mypy", ".")


def test_declared_python_analyzer_reports_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    inspection = inspect_static_analysis(tmp_path)

    assert len(inspection.analyzers) == 1
    assert inspection.analyzers[0].name == "ruff"
    assert not inspection.analyzers[0].available
    assert inspection.available_steps == ()
    assert "not found" in inspection.analyzers[0].availability


def test_project_venv_analyzer_uses_project_relative_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    executable = tmp_path / ".venv" / (
        "Scripts/ruff.exe" if os.name == "nt" else "bin/ruff"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("placeholder", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    inspection = inspect_static_analysis(tmp_path)

    analyzer = inspection.analyzers[0]
    assert analyzer.available
    assert analyzer.availability == "project-venv"
    assert analyzer.step.argv[0] == executable.relative_to(tmp_path).as_posix()


def test_path_analyzer_is_preferred_for_isolated_workspace_portability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    executable = tmp_path / ".venv" / (
        "Scripts/ruff.exe" if os.name == "nt" else "bin/ruff"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("placeholder", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/active-environment/{name}" if name == "ruff" else None,
    )

    analyzer = inspect_static_analysis(tmp_path).analyzers[0]

    assert analyzer.availability == "PATH"
    assert analyzer.step.argv == ("ruff", "check", ".")


def test_detects_declared_local_eslint_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"lint": "eslint src"},
                "devDependencies": {"eslint": "^9.0.0"},
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "node_modules" / ".bin" / (
        "eslint.cmd" if os.name == "nt" else "eslint"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("placeholder", encoding="utf-8")
    if os.name != "nt":
        binary.chmod(0o755)
    manager = "npm.cmd" if os.name == "nt" else "npm"
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == manager else None,
    )

    inspection = inspect_static_analysis(tmp_path)

    analyzer = inspection.analyzers[0]
    assert analyzer.name == "eslint"
    assert analyzer.available
    assert analyzer.step.argv == (manager, "run", "lint")


def test_eslint_config_without_script_uses_local_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"eslint": "^9.0.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    binary = tmp_path / "node_modules" / ".bin" / (
        "eslint.cmd" if os.name == "nt" else "eslint"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("placeholder", encoding="utf-8")
    if os.name != "nt":
        binary.chmod(0o755)
    manager = "npm.cmd" if os.name == "nt" else "npm"
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda _: None,
    )

    inspection = inspect_static_analysis(tmp_path)

    analyzer = inspection.analyzers[0]
    assert analyzer.available
    assert analyzer.markers == ("package.json", "eslint.config.js")
    assert analyzer.step.argv == (binary.relative_to(tmp_path).as_posix(), ".")


def test_clippy_requires_explicit_lint_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        """[package]
name = "sample"
version = "0.1.0"

[lints.clippy]
pedantic = "warn"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}"
        if name in {"cargo", "cargo-clippy"}
        else None,
    )

    inspection = inspect_static_analysis(tmp_path)

    assert inspection.analyzers[0].name == "clippy"
    assert inspection.analyzers[0].step.argv == (
        "cargo",
        "clippy",
        "--all-targets",
    )


def test_analyze_cli_runs_only_available_declared_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == "ruff" else None,
    )

    def fake_run(
        root: Path,
        steps: tuple[object, ...],
        *,
        timeout_seconds: float,
        save_report: bool,
    ) -> BuildResult:
        assert Path(root) == tmp_path.resolve()
        assert len(steps) == 1
        assert timeout_seconds == 300
        assert not save_report
        return BuildResult(
            tmp_path.resolve(),
            (
                StepResult(
                    "Run Ruff static analysis",
                    ("ruff", "check", "."),
                    0,
                    "All checks passed!\n",
                    "",
                    0.1,
                ),
            ),
            "2026-10-29T00:00:00+00:00",
        )

    monkeypatch.setattr("autofix.cli.run_build", fake_run)

    result = runner.invoke(
        app,
        ["analyze", str(tmp_path), "--run", "--json", "--no-save"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "autofix-static-analysis"
    assert payload["version"] == 1
    assert payload["available_count"] == 1
    assert payload["run"]["succeeded"] is True


def test_committed_static_analysis_schema_matches_cli_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)
    result = runner.invoke(app, ["analyze", str(tmp_path), "--json"])
    payload = json.loads(result.stdout)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-static-analysis-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert schema["properties"]["version"]["const"] == payload["version"]
    assert set(schema["required"]) == set(payload)
    assert set(schema["$defs"]["analyzer"]["required"]) == set(
        payload["analyzers"][0]
    )


def test_analyze_cli_refuses_run_when_declared_tool_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    result = runner.invoke(app, ["analyze", str(tmp_path), "--run", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["declared_count"] == 1
    assert payload["available_count"] == 0
    assert payload["run"] is None


def test_run_can_append_declared_static_analysis_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == "ruff" else None,
    )

    result = runner.invoke(
        app,
        ["run", str(tmp_path), "--with-static-analysis", "--dry-run"],
    )

    assert result.exit_code == 0, result.stdout
    assert "python main.py" in result.stdout
    assert "ruff check ." in result.stdout
    assert result.stdout.index("python main.py") < result.stdout.index("ruff check .")


def test_run_static_analysis_binding_never_silently_skips_missing_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    result = runner.invoke(
        app,
        ["run", str(tmp_path), "--with-static-analysis", "--dry-run"],
    )

    assert result.exit_code == 2
    output = " ".join(result.stdout.split())
    assert "requires every declared analyzer" in output
    assert "ruff: executable not found" in output


def test_run_static_analysis_binding_deduplicates_explicit_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == "ruff" else None,
    )

    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--step",
            "ruff check .",
            "--with-static-analysis",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout.count("ruff check .") == 1


def test_run_static_analysis_binding_requires_project_declaration(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["run", str(tmp_path), "--with-static-analysis", "--dry-run"],
    )

    assert result.exit_code == 2
    assert "found no supported analyzer declared" in " ".join(result.stdout.split())


def test_run_static_analysis_binding_rejects_host_discovery_for_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == "ruff" else None,
    )

    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--with-static-analysis",
            "--docker",
            "--dry-run",
        ],
    )

    assert result.exit_code == 2
    assert "currently supports only the local runner" in " ".join(
        result.stdout.split()
    )


def test_repair_static_analysis_binding_rejects_missing_tool_before_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    result = runner.invoke(
        app,
        ["repair", str(tmp_path), "--with-static-analysis"],
    )

    assert result.exit_code == 2
    assert "requires every declared analyzer" in " ".join(result.stdout.split())


def test_repair_static_analysis_rejects_project_local_tool_missing_from_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
    executable = tmp_path / ".venv" / (
        "Scripts/ruff.exe" if os.name == "nt" else "bin/ruff"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("placeholder", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    monkeypatch.setattr("autofix.static_analysis.shutil.which", lambda _: None)

    result = runner.invoke(
        app,
        ["repair", str(tmp_path), "--with-static-analysis"],
    )

    assert result.exit_code == 2
    output = " ".join(result.stdout.split())
    assert "detached worktree" in output
    assert "activate Python tools on PATH" in output


def test_committed_static_analysis_fixture_separates_build_and_lint_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "static-analysis"
        / "python-unused-import"
    )
    monkeypatch.setattr(
        "autofix.static_analysis.shutil.which",
        lambda name: f"/tools/{name}" if name == "ruff" else None,
    )

    inspection = inspect_static_analysis(fixture)
    result = runner.invoke(
        app,
        ["run", str(fixture), "--with-static-analysis", "--dry-run"],
    )

    assert [analyzer.name for analyzer in inspection.analyzers] == ["ruff"]
    assert result.exit_code == 0, result.stdout
    assert result.stdout.index("python -m compileall -q main.py") < result.stdout.index(
        "ruff check ."
    )
