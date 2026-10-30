from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.agent.ollama import (
    FileChange,
    PropertyCandidateProposal,
    RepairProposal,
)
from autofix.agent.patches import validate_changes
from autofix.agent.plans import save_repair_plan
from autofix.builders import BuildResult, StepResult
from autofix.cli import app
from autofix.models import BuildStep
from autofix.property_testing import (
    PropertyCheckError,
    check_property_candidate,
    generate_property_candidate,
    inspect_property_candidate,
    promote_property_candidate,
)


runner = CliRunner()


def _project_and_plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "calculator.py"
    source.write_text("def absolute(value):\n    return value\n", encoding="utf-8")
    proposal = RepairProposal(
        "handle negative values",
        (
            FileChange(
                "calculator.py",
                "def absolute(value):\n    return -value if value < 0 else value\n",
                "preserve the absolute-value invariant",
            ),
        ),
    )
    plan = save_repair_plan(
        project,
        model="test-model",
        summary=proposal.summary,
        changes=validate_changes(project, proposal),
        verification_steps=(
            BuildStep("Existing verification", ("python", "-m", "pytest")),
        ),
    )
    candidate = tmp_path / "candidate_property.py"
    candidate.write_text(
        """from hypothesis import given, strategies as st

from calculator import absolute


@given(st.integers())
def test_absolute_is_non_negative(value):
    assert absolute(value) >= 0
""",
        encoding="utf-8",
    )
    return project, plan, candidate


def _result(
    root: Path,
    step: BuildStep,
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> BuildResult:
    return BuildResult(
        root,
        (
            StepResult(
                step.label,
                step.argv,
                exit_code,
                stdout,
                stderr,
                0.01,
            ),
        ),
        "2026-10-30T00:00:00+00:00",
    )


def _confirmed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        step = steps[0]
        if root.name == "baseline":
            return _result(root, step, exit_code=1, stdout="stable failure\n")
        return _result(root, step, exit_code=0, stdout="passed\n")

    checked = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        build_runner=fake_runner,
    )
    assert checked.report_path is not None
    return project, plan, candidate, checked.report_path


def test_candidate_requires_hypothesis_given_and_top_level_test(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("def test_value():\n    assert True\n", encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="import Hypothesis"):
        inspect_property_candidate(candidate)


def test_candidate_rejects_obvious_side_effect_capabilities(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        """import os
from hypothesis import given, strategies as st

@given(st.integers())
def test_value(value):
    os.system(str(value))
""",
        encoding="utf-8",
    )

    with pytest.raises(PropertyCheckError, match="forbidden.*os"):
        inspect_property_candidate(candidate)


def test_preview_validates_plan_and_never_executes_or_installs_candidate(
    tmp_path: Path,
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)

    result = check_property_candidate(project, candidate, plan)

    assert result.verdict == "preview"
    assert not result.run_requested
    assert not result.passed
    assert result.baseline_runs == ()
    assert not (project / ".autofix_property_candidates").exists()
    assert "return value" in (project / "calculator.py").read_text(encoding="utf-8")


def test_differential_check_confirms_stable_counterexample_without_writing_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(
        root: Path,
        steps: tuple[BuildStep, ...],
        **_: object,
    ) -> BuildResult:
        step = steps[0]
        calls.append((root.name, step.label))
        installed = root / ".autofix_property_candidates"
        assert len(tuple(installed.glob("test_*.py"))) == 1
        if root.name == "baseline":
            return _result(root, step, exit_code=1, stdout="1 failed in 0.10s\n")
        if step.label == "Run isolated property candidate":
            fixed = (root / "calculator.py").read_text(encoding="utf-8")
            assert "value < 0" in fixed
            return _result(root, step, exit_code=0, stdout="1 passed in 0.11s\n")
        return _result(root, step, exit_code=0, stdout="existing suite passed\n")

    result = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        build_runner=fake_runner,
    )

    assert result.passed
    assert result.verdict == "counterexample-confirmed"
    assert len(result.baseline_runs) == 2
    assert len(result.fixed_runs) == 2
    assert len(result.fixed_verification) == 1
    assert len(calls) == 5
    assert result.report_path is not None and result.report_path.is_file()
    assert not (project / ".autofix_property_candidates").exists()
    assert (project / "calculator.py").read_text(encoding="utf-8").endswith(
        "return value\n"
    )


def test_differential_check_rejects_candidate_that_passes_on_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        return _result(root, steps[0], exit_code=0, stdout="passed\n")

    result = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        save_report=False,
        build_runner=fake_runner,
    )

    assert result.verdict == "baseline-not-failing"
    assert not result.passed
    assert result.fixed_runs == ()


def test_differential_check_rejects_unstable_baseline_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )
    count = 0

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        nonlocal count
        count += 1
        return _result(root, steps[0], exit_code=1, stdout=f"counterexample {count}\n")

    result = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        save_report=False,
        build_runner=fake_runner,
    )

    assert result.verdict == "baseline-unstable"
    assert result.fixed_runs == ()


def test_differential_check_requires_candidate_and_existing_matrix_on_fixed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        step = steps[0]
        if root.name == "baseline":
            return _result(root, step, exit_code=1, stdout="stable failure\n")
        if step.label == "Run isolated property candidate":
            return _result(root, step, exit_code=0, stdout="candidate passed\n")
        return _result(root, step, exit_code=3, stderr="regression\n")

    result = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        save_report=False,
        build_runner=fake_runner,
    )

    assert result.verdict == "fixed-verification-failed"
    assert not result.passed
    assert result.fixed_verification[-1].exit_code == 3


def test_differential_check_rejects_candidate_that_still_fails_after_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        return _result(root, steps[0], exit_code=1, stdout="stable failure\n")

    result = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        save_report=False,
        build_runner=fake_runner,
    )

    assert result.verdict == "fixed-candidate-failed"
    assert len(result.baseline_runs) == 2
    assert len(result.fixed_runs) == 2
    assert result.fixed_verification == ()


def test_committed_property_fixture_has_valid_candidate_and_hash_bound_plan() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "property-testing"
        / "python-absolute"
    )

    result = check_property_candidate(
        fixture / "project",
        fixture / "candidate_property.py",
        fixture / "repair-plan.json",
    )

    assert result.verdict == "preview"
    assert result.candidate.test_functions == ("test_absolute_is_never_negative",)
    assert result.command.argv[-2].startswith(".autofix_property_candidates/test_")


def test_property_check_cli_preview_json_matches_committed_schema(tmp_path: Path) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)

    result = runner.invoke(
        app,
        [
            "property-check",
            str(candidate),
            "--project",
            str(project),
            "--plan",
            str(plan),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-property-check-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert schema["properties"]["version"]["const"] == payload["version"]
    assert set(schema["required"]) == set(payload)
    assert payload["verdict"] == "preview"


def test_generation_saves_validated_candidate_only_under_autofix(
    tmp_path: Path,
) -> None:
    project, plan, _ = _project_and_plan(tmp_path)

    class Client:
        def generate_property_candidate(self, **_: object) -> PropertyCandidateProposal:
            return PropertyCandidateProposal(
                "cover negative integers",
                "absolute values are non-negative",
                """from hypothesis import given, strategies as st

from calculator import absolute


@given(st.integers())
def test_generated_non_negative(value):
    assert absolute(value) >= 0
""",
            )

    result = generate_property_candidate(
        project,
        plan,
        Client(),  # type: ignore[arg-type]
        model="test-model",
    )

    safe_dir = project / ".autofix" / "property-candidates"
    assert result.candidate.path.parent == safe_dir
    assert result.candidate.path.is_file()
    assert result.metadata_path.parent == safe_dir
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["executed"] is False
    assert metadata["candidate"]["sha256"] == result.candidate.sha256
    assert not (project / "tests").exists()
    assert "return value" in (project / "calculator.py").read_text(encoding="utf-8")


def test_generation_rejects_invalid_model_source_before_writing_artifact(
    tmp_path: Path,
) -> None:
    project, plan, _ = _project_and_plan(tmp_path)

    class Client:
        def generate_property_candidate(self, **_: object) -> PropertyCandidateProposal:
            return PropertyCandidateProposal("guess", "example", "assert True\n")

    with pytest.raises(PropertyCheckError, match="import Hypothesis"):
        generate_property_candidate(
            project,
            plan,
            Client(),  # type: ignore[arg-type]
            model="test-model",
        )

    assert not (project / ".autofix" / "property-candidates").exists()


def test_property_generate_cli_json_matches_committed_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, _ = _project_and_plan(tmp_path)

    def fake_generate(self: object, **_: object) -> PropertyCandidateProposal:
        return PropertyCandidateProposal(
            "cover negative integers",
            "absolute values are non-negative",
            """from hypothesis import given, strategies as st
from calculator import absolute

@given(st.integers())
def test_generated_non_negative(value):
    assert absolute(value) >= 0
""",
        )

    monkeypatch.setattr(
        "autofix.agent.ollama.OllamaClient.generate_property_candidate",
        fake_generate,
    )
    result = runner.invoke(
        app,
        [
            "property-generate",
            str(project),
            "--plan",
            str(plan),
            "--model",
            "test-model",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-property-candidate-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
    assert payload["executed"] is False


def test_promote_confirmed_candidate_creates_unapplied_git_compatible_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)

    promoted = promote_property_candidate(project, report)

    assert promoted.target_path.startswith("tests/test_autofix_property_")
    assert not (project / promoted.target_path).exists()
    patch = promoted.patch_path.read_text(encoding="utf-8")
    assert f"diff --git a/{promoted.target_path} b/{promoted.target_path}" in patch
    assert "--- /dev/null" in patch
    assert "from hypothesis import given" in patch
    metadata = json.loads(promoted.metadata_path.read_text(encoding="utf-8"))
    assert metadata["applied"] is False
    assert metadata["patch_sha256"] == promoted.patch_sha256
    checked = subprocess.run(
        ["git", "apply", "--check", str(promoted.patch_path)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_promote_preserves_candidate_without_final_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    candidate.write_text(
        candidate.read_text(encoding="utf-8").rstrip("\n"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autofix.property_testing.importlib.util.find_spec", lambda _: object()
    )

    def fake_runner(root: Path, steps: tuple[BuildStep, ...], **_: object) -> BuildResult:
        exit_code = 1 if root.name == "baseline" else 0
        return _result(root, steps[0], exit_code=exit_code, stdout="stable\n")

    checked = check_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        build_runner=fake_runner,
    )
    assert checked.report_path is not None

    promoted = promote_property_candidate(project, checked.report_path)

    patch = promoted.patch_path.read_text(encoding="utf-8")
    assert "\\ No newline at end of file" in patch
    result = subprocess.run(
        ["git", "apply", "--check", str(promoted.patch_path)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_promote_rejects_unconfirmed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["passed"] = False
    payload["verdict"] = "fixed-candidate-failed"
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="counterexample-confirmed"):
        promote_property_candidate(project, report)


def test_promote_rejects_incomplete_claimed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["fixed_verification"] = []
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="complete confirmed"):
        promote_property_candidate(project, report)


def test_promote_rejects_candidate_changed_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, candidate, report = _confirmed_report(tmp_path, monkeypatch)
    candidate.write_text(candidate.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="changed after"):
        promote_property_candidate(project, report)


@pytest.mark.parametrize("target", ["../escape.py", "src/property.py", "tests/data.txt"])
def test_promote_rejects_unsafe_or_non_test_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)

    with pytest.raises(PropertyCheckError, match="target"):
        promote_property_candidate(project, report, target=target)


def test_promote_refuses_to_overwrite_existing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)
    existing = project / "tests" / "test_existing.py"
    existing.parent.mkdir()
    existing.write_text("def test_existing(): assert True\n", encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="already exists"):
        promote_property_candidate(
            project,
            report,
            target="tests/test_existing.py",
        )


def test_property_promote_cli_json_matches_committed_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "property-promote",
            str(report),
            "--project",
            str(project),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-property-promotion-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
    assert payload["applied"] is False
