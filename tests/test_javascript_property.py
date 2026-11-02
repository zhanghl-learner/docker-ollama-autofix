from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import validate_changes
from autofix.agent.plans import save_repair_plan
from autofix.agent.textio import write_utf8_exact
from autofix.cli import app
from autofix.javascript_property import (
    apply_javascript_property_promotion,
    check_javascript_property_candidate,
    inspect_javascript_property_candidate,
    promote_javascript_property_candidate,
)
from autofix.builders import BuildResult, StepResult
from autofix.models import BuildStep
from autofix.property_testing import PropertyCheckError
from autofix.runtime.tools import find_executable


runner = CliRunner()


def _candidate_source() -> str:
    return """import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';

import { absolute } from '../calculator.js';

test('absolute is never negative', () => {
  fc.assert(fc.property(fc.integer(), (value) => {
    assert.ok(absolute(value) >= 0);
  }));
});
"""


def _project_and_plan(
    tmp_path: Path,
    *,
    dependencies: bool = True,
    dependency_layout: str = "hoisted",
) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "calculator.js").write_text(
        "export function absolute(value) {\n  return value;\n}\n",
        encoding="utf-8",
    )
    package = {
        "name": "autofix-fast-check-fixture",
        "private": True,
        "type": "module",
    }
    if dependencies:
        package["devDependencies"] = {"fast-check": "0.0.0-fixture"}
    (project / "package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )
    module = project / "node_modules" / "fast-check"
    module.mkdir(parents=True)
    (module / "package.json").write_text(
        json.dumps(
            {
                "name": "fast-check",
                "version": "0.0.0-fixture",
                "type": "module",
                "exports": "./index.js",
                "dependencies": {"pure-rand": "0.0.0-fixture"},
                "optionalDependencies": {"optional-addon": "0.0.0-fixture"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (module / "index.js").write_text(
        """import { values } from 'pure-rand';
export function integer() { return {}; }
export function property(_arbitrary, predicate) { return predicate; }
export function assert(predicate) {
  for (const value of values) {
    predicate(value);
  }
}
export default { integer, property, assert };
""",
        encoding="utf-8",
    )
    if dependency_layout not in {"hoisted", "nested"}:
        raise ValueError(f"unsupported fixture dependency layout: {dependency_layout}")
    dependency_parent = (
        project / "node_modules"
        if dependency_layout == "hoisted"
        else module / "node_modules"
    )
    pure_rand = dependency_parent / "pure-rand"
    pure_rand.mkdir(parents=True)
    (pure_rand / "package.json").write_text(
        json.dumps(
            {
                "name": "pure-rand",
                "version": "0.0.0-fixture",
                "type": "module",
                "exports": "./index.js",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (pure_rand / "index.js").write_text(
        "export const values = [-2, -1, 0, 1, 2];\n", encoding="utf-8"
    )
    proposal = RepairProposal(
        "preserve absolute-value invariant",
        (
            FileChange(
                "calculator.js",
                "export function absolute(value) {\n"
                "  return value < 0 ? -value : value;\n"
                "}\n",
                "handle negative values",
            ),
        ),
    )
    plan = save_repair_plan(
        project,
        model="fixture",
        summary=proposal.summary,
        changes=validate_changes(project, proposal),
        verification_steps=(
            BuildStep(
                "Verify fixed JavaScript module",
                (
                    "node",
                    "--input-type=module",
                    "-e",
                    "import('./calculator.js').then(({absolute}) => { "
                    "if (absolute(-2) !== 2) process.exit(1); })",
                ),
            ),
        ),
    )
    candidate = tmp_path / "candidate_property.mjs"
    candidate.write_text(_candidate_source(), encoding="utf-8")
    return project, plan, candidate


def _confirmed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    project, plan, candidate = _project_and_plan(tmp_path)
    monkeypatch.setattr(
        "autofix.javascript_property.find_executable", lambda _: "node"
    )

    def fake_runner(
        root: Path, steps: tuple[BuildStep, ...], **_: object
    ) -> BuildResult:
        step = steps[0]
        failed = root.name == "baseline"
        evidence = StepResult(
            step.label,
            step.argv,
            1 if failed else 0,
            "stable counterexample\n" if failed else "passed\n",
            "",
            0.01,
        )
        return BuildResult(root, (evidence,), "2026-11-02T00:00:00+00:00")

    checked = check_javascript_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        build_runner=fake_runner,
    )
    assert checked.report_path is not None
    assert checked.verdict == "counterexample-confirmed"
    return project, plan, candidate, checked.report_path


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one deterministic Git fixture command and require success."""

    return subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _ready_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """Create reviewed evidence, then commit the exact repaired JavaScript source."""

    project, plan, _, report = _confirmed_report(tmp_path, monkeypatch)
    promotion = promote_javascript_property_candidate(project, report)
    plan_data = json.loads(plan.read_text(encoding="utf-8"))
    write_utf8_exact(
        project / "calculator.js", plan_data["changes"][0]["content"]
    )
    (project / ".gitignore").write_text(
        ".autofix/\nnode_modules/\n", encoding="utf-8"
    )
    _git(project, "init")
    _git(project, "config", "user.email", "autofix@example.invalid")
    _git(project, "config", "user.name", "AutoFix Test")
    _git(project, "add", "calculator.js", "package.json", ".gitignore")
    _git(project, "commit", "-m", "Apply reviewed repair")
    return project, promotion.metadata_path, promotion.target_path


def test_javascript_candidate_preview_is_static_and_non_mutating(tmp_path: Path) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)

    result = check_javascript_property_candidate(project, candidate, plan)

    assert result.verdict == "preview"
    assert result.candidate.to_dict()["framework"] == "fast-check"
    assert result.command.argv[0] == "node"
    assert not (project / result.candidate.target_path).exists()


def test_javascript_candidate_rejects_side_effect_modules(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.mjs"
    candidate.write_text(
        _candidate_source() + "\nimport { readFile } from 'node:fs/promises';\n",
        encoding="utf-8",
    )

    with pytest.raises(PropertyCheckError, match="forbidden capability: fs"):
        inspect_javascript_property_candidate(candidate)


def test_javascript_execution_requires_declared_fast_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path, dependencies=False)
    monkeypatch.setattr(
        "autofix.javascript_property.find_executable", lambda _: "node"
    )

    with pytest.raises(PropertyCheckError, match="declared in package.json"):
        check_javascript_property_candidate(project, candidate, plan, run=True)


@pytest.mark.skipif(find_executable("node") is None, reason="Node.js is not installed")
@pytest.mark.parametrize("dependency_layout", ["hoisted", "nested"])
def test_javascript_property_real_differential_loop(
    tmp_path: Path, dependency_layout: str
) -> None:
    project, plan, candidate = _project_and_plan(
        tmp_path, dependency_layout=dependency_layout
    )

    result = check_javascript_property_candidate(
        project,
        candidate,
        plan,
        run=True,
        save_report=False,
    )

    assert result.verdict == "counterexample-confirmed"
    assert len(result.baseline_runs) == 2
    assert all(not item.succeeded for item in result.baseline_runs)
    assert len({item.failure_signature for item in result.baseline_runs}) == 1
    assert len(result.fixed_runs) == 2
    assert all(item.succeeded for item in result.fixed_runs)
    assert all(item.succeeded for item in result.fixed_verification)
    packages = {item.relative_path: item for item in result.dependency_packages}
    assert "node_modules/fast-check" in packages
    pure_rand_path = (
        "node_modules/pure-rand"
        if dependency_layout == "hoisted"
        else "node_modules/fast-check/node_modules/pure-rand"
    )
    assert packages[pure_rand_path].version == "0.0.0-fixture"
    assert all(len(item.package_json_sha256) == 64 for item in packages.values())
    assert not (project / result.candidate.target_path).exists()


def test_javascript_dependency_rejects_unsafe_transitive_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)
    manifest = project / "node_modules" / "fast-check" / "package.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["dependencies"] = {"../escape": "1.0.0"}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        "autofix.javascript_property.find_executable", lambda _: "node"
    )

    with pytest.raises(PropertyCheckError, match="unsafe dependency name"):
        check_javascript_property_candidate(project, candidate, plan, run=True)


def test_javascript_property_cli_json_matches_schema(tmp_path: Path) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)

    result = runner.invoke(
        app,
        [
            "property-js-check",
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
        / "autofix-javascript-property-check-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
    assert payload["version"] == 2
    assert payload["dependency_packages"] == []


def test_javascript_promotion_emits_unapplied_patch_bound_to_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)

    result = promote_javascript_property_candidate(project, report)

    assert result.patch_path.is_file()
    assert result.metadata_path.is_file()
    assert result.target_path.endswith(".test.mjs")
    assert not (project / result.target_path).exists()
    patch = result.patch_path.read_text(encoding="utf-8")
    assert "new file mode 100644" in patch
    assert f"b/{result.target_path}" in patch
    checked = subprocess.run(
        ["git", "apply", "--check", str(result.patch_path)],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["applied"] is False
    assert metadata["dependency_packages"]
    assert metadata["patch_sha256"] == result.patch_sha256


def test_javascript_promotion_rejects_dependency_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)
    manifest = project / "node_modules" / "fast-check" / "package.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "0.0.1-drifted"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="dependency closure changed"):
        promote_javascript_property_candidate(project, report)


def test_javascript_promotion_rejects_incomplete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)
    data = json.loads(report.read_text(encoding="utf-8"))
    data["fixed_runs"] = data["fixed_runs"][:1]
    report.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PropertyCheckError, match="complete confirmed"):
        promote_javascript_property_candidate(project, report)


def test_javascript_promotion_cli_json_matches_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _, report = _confirmed_report(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "property-js-promote",
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
        / "autofix-javascript-property-promotion-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
    assert payload["applied"] is False


def test_javascript_apply_preview_is_non_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, promotion, target = _ready_promotion(tmp_path, monkeypatch)

    result = apply_javascript_property_promotion(project, promotion)

    assert result.apply_requested is False
    assert result.applied is False
    assert result.verification is None
    assert result.verification_steps[0].argv[1:3] == (
        "--test",
        "--test-isolation=none",
    )
    assert not (project / target).exists()


def test_javascript_apply_verifies_in_worktree_and_writes_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, promotion, target = _ready_promotion(tmp_path, monkeypatch)
    observed_root: Path | None = None

    def fake_runner(
        root: Path, steps: tuple[BuildStep, ...], **_: object
    ) -> BuildResult:
        nonlocal observed_root
        observed_root = root
        assert root != project
        assert (root / target).is_file()
        assert (root / "node_modules" / "fast-check" / "package.json").is_file()
        assert (root / "node_modules" / "pure-rand" / "package.json").is_file()
        (root / "worktree-side-effect.txt").write_text("isolated", encoding="utf-8")
        evidence = tuple(
            StepResult(step.label, step.argv, 0, "passed\n", "", 0.01)
            for step in steps
        )
        return BuildResult(root, evidence, "2026-11-02T00:00:00+00:00")

    result = apply_javascript_property_promotion(
        project, promotion, apply=True, build_runner=fake_runner
    )

    assert observed_root is not None
    assert result.applied is True
    assert result.rolled_back is False
    assert (project / target).is_file()
    assert not (project / "worktree-side-effect.txt").exists()
    assert result.receipt_path is not None and result.receipt_path.is_file()
    payload = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "autofix-javascript-property-apply"
    assert payload["applied"] is True


def test_javascript_apply_verification_failure_does_not_write_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, promotion, target = _ready_promotion(tmp_path, monkeypatch)

    def failing_runner(
        root: Path, steps: tuple[BuildStep, ...], **_: object
    ) -> BuildResult:
        step = steps[0]
        evidence = StepResult(step.label, step.argv, 1, "", "failed\n", 0.01)
        return BuildResult(root, (evidence,), "2026-11-02T00:00:00+00:00")

    result = apply_javascript_property_promotion(
        project, promotion, apply=True, build_runner=failing_runner
    )

    assert result.applied is False
    assert result.rolled_back is True
    assert not (project / target).exists()
    assert result.receipt_path is not None and result.receipt_path.is_file()


def test_javascript_apply_rejects_git_ignored_target_and_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, promotion, target = _ready_promotion(tmp_path, monkeypatch)
    (project / ".gitignore").write_text(
        ".autofix/\nnode_modules/\ntests/\n", encoding="utf-8"
    )
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-m", "Ignore generated tests")

    def fake_runner(
        root: Path, steps: tuple[BuildStep, ...], **_: object
    ) -> BuildResult:
        evidence = tuple(
            StepResult(step.label, step.argv, 0, "passed\n", "", 0.01)
            for step in steps
        )
        return BuildResult(root, evidence, "2026-11-02T00:00:00+00:00")

    with pytest.raises(PropertyCheckError, match="not exactly the new test file"):
        apply_javascript_property_promotion(
            project, promotion, apply=True, build_runner=fake_runner
        )
    assert not (project / target).exists()


@pytest.mark.skipif(find_executable("node") is None, reason="Node.js is not installed")
def test_javascript_apply_cli_real_worktree_json_matches_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, promotion, target = _ready_promotion(tmp_path, monkeypatch)
    receipt = tmp_path / "javascript-apply-receipt.json"
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (source_root, environment.get("PYTHONPATH", "")))
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autofix",
            "property-js-apply",
            str(promotion),
            "--project",
            str(project),
            "--apply",
            "--output",
            str(receipt),
            "--json",
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-javascript-property-apply-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
    assert payload["applied"] is True
    assert len(payload["verification_steps"]) == 2
    assert payload["receipt_path"] == str(receipt.resolve())
    assert (project / target).is_file()
