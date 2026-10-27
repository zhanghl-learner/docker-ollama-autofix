from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import apply_changes, validate_changes
from autofix.agent.plans import save_repair_plan
from autofix.builders import DockerConfig
from autofix.cli import app
from autofix.config import load_config
from autofix.models import BuildStep


runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "autofix 0.19.0" in result.stdout


def test_evaluate_confirms_intentionally_failing_baseline(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    (tmp_path / "autofix-eval.yml").write_text(
        """version: 1
name: cli-baseline
language: Python
difficulty: basic
commands: [python broken.py]
allowed_change_paths: [broken.py]
required_change_paths: [broken.py]
tags: [cli]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["evaluate", str(tmp_path), "--no-save"])

    assert result.exit_code == 0, result.stdout
    assert "Baseline is valid" in result.stdout


def test_evaluate_rejects_wrong_failure_signature(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(
        "print('environment failure')\nraise SystemExit(1)\n", encoding="utf-8"
    )
    (tmp_path / "autofix-eval.yml").write_text(
        """version: 1
name: wrong-failure
language: Python
difficulty: basic
commands: [python broken.py]
expected_failure:
  exit_code: 1
  failed_step: 1
  output_contains: [expected business failure]
allowed_change_paths: [broken.py]
required_change_paths: [broken.py]
tags: [cli]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["evaluate", str(tmp_path), "--no-save"])

    assert result.exit_code == 1
    assert "failure contract" in result.stdout
    assert "mismatched" in result.stdout
    assert "required output is missing" in result.stdout


def test_configure_saves_user_defaults_for_new_projects(tmp_path: Path) -> None:
    project = tmp_path / "fresh-project"
    project.mkdir()

    result = runner.invoke(
        app,
        [
            "configure",
            "--model",
            "qwen3:0.6b",
            "--docker-image",
            "autofix-python:3.11",
            "--offline",
        ],
    )

    assert result.exit_code == 0, result.stdout
    config = load_config(project)
    assert config is not None
    assert config.ollama_model == "qwen3:0.6b"
    assert config.docker_image == "autofix-python:3.11"
    assert config.offline is True


def test_inspect_json(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(tmp_path), "--json"])

    assert result.exit_code == 0
    assert '"language": "Python"' in result.stdout


def test_run_custom_command(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", str(tmp_path), "--command", "python -c \"print('built')\"", "--no-save"],
    )

    assert result.exit_code == 0
    assert "built" in result.stdout


def test_docker_dry_run_shows_security_plan(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = runner.invoke(app, ["run", str(tmp_path), "--docker", "--dry-run", "--json"])

    assert result.exit_code == 0
    assert '"docker_disposable_copy": true' in result.stdout
    assert '"--network"' in result.stdout
    assert '"none"' in result.stdout
    assert '"--pull"' in result.stdout
    assert '"never"' in result.stdout


def test_run_uses_manifest_command(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python -c \"print('manifest')\"}\nruntime: {}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["run", str(tmp_path), "--no-save"])

    assert result.exit_code == 0
    assert "manifest" in result.stdout


def test_manifest_offline_policy_rejects_network(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build: {command: python -V}
runtime:
  docker_image: python:3.11-slim
  offline: true
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["run", str(tmp_path), "--docker", "--allow-network", "--dry-run"]
    )

    assert result.exit_code == 2
    assert "conflicts with runtime.offline" in result.stdout


def test_history_lists_repair_reports(tmp_path: Path) -> None:
    report_dir = tmp_path / ".autofix" / "repairs"
    report_dir.mkdir(parents=True)
    report = {
        "created_at": "2026-10-21T00:00:00+00:00",
        "succeeded": True,
        "applied": True,
        "rolled_back": False,
        "proposals": [{"summary": "fix"}],
        "rejections": [],
        "error": None,
    }
    (report_dir / "20261021T000000.000000Z.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    result = runner.invoke(app, ["history", str(tmp_path), "--json"])

    assert result.exit_code == 0
    assert '"succeeded": true' in result.stdout
    assert "20261021T000000.000000Z.json" in result.stdout


def test_rollback_command_restores_recorded_change(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "change value", (FileChange("main.py", "value = 2\n", "change value"),)
    )
    change = validate_changes(tmp_path, proposal)[0]
    apply_changes((change,))
    report = tmp_path / ".autofix" / "repairs" / "repair.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "version": 1,
                "type": "repair",
                "changes": [
                    {"path": change.relative_path, "reason": change.reason, "diff": change.diff}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["rollback", str(report), "--project", str(tmp_path)]
    )

    assert result.exit_code == 0, result.stdout
    assert "Rollback completed" in result.stdout
    assert "Rollback receipt" in result.stdout
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_undo_restores_latest_applied_repair_once(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "change value", (FileChange("main.py", "value = 2\n", "change value"),)
    )
    change = validate_changes(tmp_path, proposal)[0]
    apply_changes((change,))
    report = tmp_path / ".autofix" / "repairs" / "20261021T000000.000000Z.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "version": 1,
                "type": "repair",
                "succeeded": True,
                "applied": True,
                "changes": [
                    {"path": change.relative_path, "reason": change.reason, "diff": change.diff}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["undo", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "Undo completed" in result.stdout
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    history_result = runner.invoke(app, ["history", str(tmp_path), "--json"])
    assert '"undone": true' in history_result.stdout
    second = runner.invoke(app, ["undo", str(tmp_path)])
    assert second.exit_code == 2
    assert "not-yet-undone" in second.stdout


def test_apply_plan_uses_reviewed_content_without_model(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python -m pytest}\nruntime: {}\n",
        encoding="utf-8",
    )
    proposal = RepairProposal(
        "fix addition",
        (
            FileChange(
                "calculator.py",
                "def add(left: int, right: int) -> int:\n    return left + right\n",
                "use addition",
            ),
        ),
    )
    plan = save_repair_plan(
        tmp_path,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
    )
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python -c \\\"raise SystemExit(9)\\\"}\nruntime: {}\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["apply-plan", str(plan), "--project", str(tmp_path)]
    )

    assert result.exit_code == 0, result.stdout
    assert "bound to repair plan v3" in result.stdout
    assert "applied and verified successfully" in result.stdout
    assert "return left + right" in source.read_text(encoding="utf-8")


def test_apply_plan_rejects_verification_command_drift(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        (FileChange("calculator.py", "def add(a, b):\n    return a + b\n", "fix"),),
    )
    plan = save_repair_plan(
        tmp_path,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Original test", ("python", "-m", "pytest")),),
    )

    result = runner.invoke(
        app,
        [
            "apply-plan",
            str(plan),
            "--project",
            str(tmp_path),
            "--step",
            "python -m compileall -q .",
        ],
    )

    assert result.exit_code == 2
    assert "Verification commands differ" in result.stdout
    assert "return a - b" in source.read_text(encoding="utf-8")


def test_apply_plan_rejects_verification_runner_drift(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        (FileChange("calculator.py", "def add(a, b):\n    return a + b\n", "fix"),),
    )
    plan = save_repair_plan(
        tmp_path,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
    )

    result = runner.invoke(
        app, ["apply-plan", str(plan), "--project", str(tmp_path), "--docker"]
    )

    assert result.exit_code == 2
    assert "runner differs" in result.stdout
    assert "return a - b" in source.read_text(encoding="utf-8")


def test_apply_plan_rejects_bound_docker_setting_drift(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        (FileChange("calculator.py", "def add(a, b):\n    return a + b\n", "fix"),),
    )
    plan = save_repair_plan(
        tmp_path,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
        verification_runner="docker",
        docker_config=DockerConfig("python:3.11-slim"),
    )

    result = runner.invoke(
        app,
        [
            "apply-plan",
            str(plan),
            "--project",
            str(tmp_path),
            "--docker-image",
            "python:3.12-slim",
        ],
    )

    assert result.exit_code == 2
    assert "image differs" in result.stdout
    assert "return a - b" in source.read_text(encoding="utf-8")


def test_apply_plan_rejects_verification_timeout_drift(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        (FileChange("calculator.py", "def add(a, b):\n    return a + b\n", "fix"),),
    )
    plan = save_repair_plan(
        tmp_path,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
        verification_timeout_seconds=30,
    )

    result = runner.invoke(
        app,
        [
            "apply-plan",
            str(plan),
            "--project",
            str(tmp_path),
            "--timeout",
            "60",
        ],
    )

    assert result.exit_code == 2
    assert "timeout differs" in result.stdout
    assert "return a - b" in source.read_text(encoding="utf-8")


def test_init_detects_python_project_and_writes_config(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path), "--model", "qwen3:0.6b"])

    assert result.exit_code == 0, result.stdout
    config = (tmp_path / "autofix.yml").read_text(encoding="utf-8")
    assert "python -m pytest" in config
    assert "system: python" in config
    assert "autofix-python:3.11" in config
    assert "qwen3:0.6b" in config
    assert "dependencies_bundled: true" in config


def test_init_does_not_overwrite_existing_config(tmp_path: Path) -> None:
    existing = tmp_path / "autofix.yml"
    existing.write_text("version: 1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "init",
            str(tmp_path),
            "--command",
            "python -V",
            "--docker-image",
            "demo:1",
            "--model",
            "demo:1",
        ],
    )

    assert result.exit_code == 2
    assert existing.read_text(encoding="utf-8") == "version: 1\n"


def test_run_upgrades_generated_compileall_to_detected_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "nnModule.py").write_text("print('runtime')\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {command: python -m compileall -q .}\nruntime: {}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["run", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "python nnModule.py" in result.stdout


def test_run_selects_explicit_build_system_in_polyglot_project(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")

    result = runner.invoke(
        app, ["run", str(tmp_path), "--build-system", "cargo", "--dry-run"]
    )

    assert result.exit_code == 0, result.stdout
    assert "cargo test" in result.stdout


def test_run_uses_configured_build_system_in_polyglot_project(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        "version: 1\nbuild: {system: cargo}\nruntime: {}\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["run", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "cargo test" in result.stdout


def test_run_rejects_undetected_build_system(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = runner.invoke(
        app, ["run", str(tmp_path), "--build-system", "cargo", "--dry-run"]
    )

    assert result.exit_code == 2
    assert "was not detected" in result.stdout
    assert "systems: python" in result.stdout


def test_inspect_json_marks_explicit_build_system_selected(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    result = runner.invoke(
        app, ["inspect", str(tmp_path), "--build-system", "go", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["selected"]["build_system"] == "go"


def test_run_executes_repeated_steps_in_order(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--step",
            "python -c \"print('first')\"",
            "--step",
            "python -c \"print('second')\"",
            "--no-save",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout.index("first") < result.stdout.index("second")
    assert "Custom command 1" in result.stdout
    assert "Custom command 2" in result.stdout


def test_run_matrix_stops_after_first_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--step",
            "python -c \"raise SystemExit(7)\"",
            "--step",
            "python -c \"print('must-not-run')\"",
            "--no-save",
        ],
    )

    assert result.exit_code == 7
    assert "must-not-run" not in result.stdout


def test_run_rejects_command_and_step_together(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--command",
            "python -V",
            "--step",
            "python -m pytest",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be used together" in result.stdout


def test_init_writes_detected_multistep_build_matrix(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8"
    )

    result = runner.invoke(
        app, ["init", str(tmp_path), "--model", "qwen3:0.6b"]
    )

    assert result.exit_code == 0, result.stdout
    config = load_config(tmp_path)
    assert config is not None
    assert config.command is None
    assert config.commands == (
        "cmake -S . -B build",
        "cmake --build build",
    )

