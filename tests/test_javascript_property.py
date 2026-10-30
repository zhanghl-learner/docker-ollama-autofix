from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import validate_changes
from autofix.agent.plans import save_repair_plan
from autofix.cli import app
from autofix.javascript_property import (
    check_javascript_property_candidate,
    inspect_javascript_property_candidate,
)
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


def _project_and_plan(tmp_path: Path, *, dependencies: bool = True) -> tuple[Path, Path, Path]:
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
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (module / "index.js").write_text(
        """export function integer() { return {}; }
export function property(_arbitrary, predicate) { return predicate; }
export function assert(predicate) {
  for (const value of [-2, -1, 0, 1, 2]) {
    predicate(value);
  }
}
export default { integer, property, assert };
""",
        encoding="utf-8",
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
def test_javascript_property_real_differential_loop(tmp_path: Path) -> None:
    project, plan, candidate = _project_and_plan(tmp_path)

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
    assert not (project / result.candidate.target_path).exists()


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
        / "autofix-javascript-property-check-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert set(schema["required"]) == set(payload)
