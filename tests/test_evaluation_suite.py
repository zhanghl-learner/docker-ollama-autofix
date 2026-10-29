from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.agent.ollama import FileEdit, RepairProposal
from autofix.cli import app
from autofix.evaluation_suite import (
    EvaluationSuiteError,
    discover_evaluation_cases,
    evaluate_suite,
)


runner = CliRunner()


class FixedRepairClient:
    """Return a minimal edit that repairs every generated arithmetic case."""

    def propose(self, **_: object) -> RepairProposal:
        return RepairProposal(
            "restore addition",
            edits=(
                FileEdit(
                    "calculator.py",
                    2,
                    1,
                    "    return left - right",
                    "    return left + right",
                    "The function must add both operands.",
                ),
            ),
            diagnosis="calculator.py line 2 subtracts instead of adding.",
        )


class RejectedRepairClient:
    """Return an edit whose exact old text cannot be anchored."""

    def propose(self, **_: object) -> RepairProposal:
        return RepairProposal(
            "unsafe edit",
            edits=(
                FileEdit(
                    "calculator.py",
                    1,
                    1,
                    "missing old text",
                    "replacement",
                    "Exercise static rejection metrics.",
                ),
            ),
            diagnosis="The proposal intentionally cannot match the source.",
        )


class FailingVerificationClient:
    """Return an applicable edit that still fails the declared build."""

    def propose(self, **_: object) -> RepairProposal:
        return RepairProposal(
            "replace subtraction with multiplication",
            edits=(
                FileEdit(
                    "calculator.py",
                    2,
                    1,
                    "    return left - right",
                    "    return left * right",
                    "Exercise verification failure metrics.",
                ),
            ),
            diagnosis="The candidate intentionally chooses the wrong operator.",
        )


def _case(root: Path, name: str, *, expected_exit: int = 1) -> Path:
    root.mkdir(parents=True)
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    (root / "check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    (root / "autofix-eval.yml").write_text(
        f"""version: 1
name: {name}
language: Python
difficulty: basic
commands:
  - python check.py
expected_failure:
  exit_code: {expected_exit}
  failed_step: 1
allowed_change_paths:
  - source.py
required_change_paths:
  - source.py
tags: [suite-test]
""",
        encoding="utf-8",
    )
    return root


def _repair_case(root: Path, name: str) -> Path:
    root.mkdir(parents=True)
    (root / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (root / "check.py").write_text(
        "from calculator import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (root / "autofix-eval.yml").write_text(
        f"""version: 1
name: {name}
language: Python
difficulty: basic
commands:
  - python check.py
expected_failure:
  exit_code: 1
  failed_step: 1
allowed_change_paths:
  - calculator.py
required_change_paths:
  - calculator.py
tags: [suite-repair-test]
""",
        encoding="utf-8",
    )
    return root


def test_discovery_is_sorted_and_skips_generated_directories(tmp_path: Path) -> None:
    second = _case(tmp_path / "z-case", "z-case")
    first = _case(tmp_path / "a-case", "a-case")
    _case(tmp_path / ".autofix" / "ignored", "ignored")
    _case(tmp_path / "node_modules" / "ignored", "ignored-node")

    discovered = discover_evaluation_cases(tmp_path)

    assert discovered == (first.resolve(), second.resolve())


def test_suite_saves_aggregate_markdown_and_every_case_json(tmp_path: Path) -> None:
    _case(tmp_path / "python-a", "python-a")
    _case(tmp_path / "python-b", "python-b")

    result = evaluate_suite(tmp_path)

    assert result.passed
    assert result.metrics["total"] == 2
    assert result.metrics["passed"] == 2
    assert result.metrics["pass_rate"] == 1.0
    assert result.json_report_path is not None and result.json_report_path.is_file()
    assert result.markdown_report_path is not None and result.markdown_report_path.is_file()
    assert all(case.report_path and case.report_path.is_file() for case in result.cases)
    aggregate = json.loads(result.json_report_path.read_text(encoding="utf-8"))
    assert aggregate["schema"] == "autofix-evaluation-suite"
    assert [case["relative_root"] for case in aggregate["cases"]] == [
        "python-a",
        "python-b",
    ]
    markdown = result.markdown_report_path.read_text(encoding="utf-8")
    assert "Passed: **2 / 2**" in markdown
    assert "`python-a`" in markdown


def test_invalid_manifest_is_retained_without_stopping_other_cases(
    tmp_path: Path,
) -> None:
    _case(tmp_path / "good", "good")
    invalid = _case(tmp_path / "invalid", "invalid")
    (invalid / "autofix-eval.yml").write_text(
        "version: 99\nname: invalid\n", encoding="utf-8"
    )

    result = evaluate_suite(tmp_path, save_report=False)

    assert not result.passed
    assert result.metrics == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "invalid_manifests": 1,
        "execution_errors": 0,
        "duplicate_case_names": 0,
        "pass_rate": 0.5,
        "by_language": {
            "Python": {"total": 1, "passed": 1, "failed": 0},
            "unknown": {"total": 1, "passed": 0, "failed": 1},
        },
        "by_difficulty": {
            "basic": {"total": 1, "passed": 1, "failed": 0},
            "unknown": {"total": 1, "passed": 0, "failed": 1},
        },
    }
    invalid_case = next(case for case in result.cases if case.relative_root == "invalid")
    assert invalid_case.status == "invalid_manifest"
    assert invalid_case.contract_sha256
    assert "version 1" in (invalid_case.error or "")


def test_contract_mismatch_is_a_failed_case_not_an_suite_exception(
    tmp_path: Path,
) -> None:
    _case(tmp_path / "mismatch", "mismatch", expected_exit=2)

    result = evaluate_suite(tmp_path, save_report=False)

    assert not result.passed
    assert result.cases[0].status == "failed"
    assert result.cases[0].result is not None
    assert result.cases[0].result.failure_contract_mismatches == (
        "exit code 1 != expected 2",
    )


def test_duplicate_case_names_are_reported_for_every_duplicate(tmp_path: Path) -> None:
    _case(tmp_path / "first", "duplicate")
    _case(tmp_path / "second", "duplicate")

    result = evaluate_suite(tmp_path, save_report=False)

    assert not result.passed
    assert {case.status for case in result.cases} == {"duplicate_case_name"}
    assert all("first, second" in (case.error or "") for case in result.cases)


def test_execution_error_is_retained_without_stopping_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case(tmp_path / "first", "first")
    _case(tmp_path / "second", "second")
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated runner failure")
        from autofix.evaluation import evaluate_project as real_evaluate_project

        return real_evaluate_project(*args, **kwargs)

    monkeypatch.setattr("autofix.evaluation_suite.evaluate_project", fail_once)

    result = evaluate_suite(tmp_path, save_report=False)

    assert calls == 2
    assert not result.passed
    assert result.metrics["execution_errors"] == 1
    assert [case.status for case in result.cases] == ["execution_error", "passed"]
    assert "simulated runner failure" in (result.cases[0].error or "")


def test_suite_requires_at_least_one_manifest(tmp_path: Path) -> None:
    with pytest.raises(EvaluationSuiteError, match="No autofix-eval.yml"):
        evaluate_suite(tmp_path)


def test_repair_suite_requires_a_non_empty_model(tmp_path: Path) -> None:
    _repair_case(tmp_path / "repair", "repair")

    with pytest.raises(EvaluationSuiteError, match="model is required"):
        evaluate_suite(
            tmp_path,
            client=FixedRepairClient(),
            model="   ",
            save_report=False,
        )


def test_evaluate_suite_cli_json_and_failed_exit_code(tmp_path: Path) -> None:
    _case(tmp_path / "passing", "passing")
    _case(tmp_path / "failing", "failing", expected_exit=2)

    result = runner.invoke(
        app,
        ["evaluate-suite", str(tmp_path), "--json", "--no-save"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema"] == "autofix-evaluation-suite"
    assert payload["metrics"]["total"] == 2
    assert payload["metrics"]["failed"] == 1
    assert len(payload["cases"]) == 2


def test_committed_suite_schema_matches_runtime_envelope(tmp_path: Path) -> None:
    _case(tmp_path / "python-a", "python-a")
    payload = evaluate_suite(tmp_path, save_report=False).to_dict()
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-evaluation-suite-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert schema["properties"]["version"]["const"] == payload["version"]
    assert set(schema["required"]) == set(payload)
    assert set(schema["properties"]["metrics"]["required"]) == set(
        payload["metrics"]
    )
    case_schema = schema["$defs"]["case"]
    assert schema["properties"]["cases"]["items"] == {"$ref": "#/$defs/case"}
    assert set(case_schema["required"]) == set(payload["cases"][0])


def test_repair_suite_scores_first_attempts_without_mutating_cases(
    tmp_path: Path,
) -> None:
    first = _repair_case(tmp_path / "first", "first")
    second = _repair_case(tmp_path / "second", "second")
    originals = {
        path: (path / "calculator.py").read_bytes() for path in (first, second)
    }

    result = evaluate_suite(
        tmp_path,
        client=FixedRepairClient(),
        model="fixed-model",
        max_attempts=3,
    )

    assert result.passed
    assert result.mode == "repair"
    assert result.to_dict()["version"] == 2
    assert result.model == "fixed-model"
    assert result.to_dict()["autofix_version"]
    assert result.metrics["repair_eligible"] == 2
    assert result.metrics["repair_attempted"] == 2
    assert result.metrics["first_attempt_passed"] == 2
    assert result.metrics["first_attempt_pass_rate"] == 1.0
    assert result.metrics["average_attempts"] == 1.0
    assert result.metrics["static_rejection_rate"] == 0.0
    assert result.metrics["verification_failure_rate"] == 0.0
    assert result.json_report_path is not None and result.json_report_path.is_file()
    assert result.markdown_report_path is not None
    markdown = result.markdown_report_path.read_text(encoding="utf-8")
    assert "Mode: `repair`" in markdown
    assert "Model: `fixed-model`" in markdown
    aggregate = json.loads(result.json_report_path.read_text(encoding="utf-8"))
    assert aggregate["version"] == 2
    assert aggregate["metrics"]["first_attempt_pass_rate"] == 1.0
    assert all(
        (path / "calculator.py").read_bytes() == content
        for path, content in originals.items()
    )


def test_repair_suite_keeps_rejection_metrics_when_reports_are_disabled(
    tmp_path: Path,
) -> None:
    _repair_case(tmp_path / "rejected", "rejected")

    result = evaluate_suite(
        tmp_path,
        client=RejectedRepairClient(),
        model="rejected-model",
        max_attempts=1,
        save_report=False,
    )

    assert not result.passed
    assert result.metrics["repair_attempted"] == 1
    assert result.metrics["total_proposals"] == 1
    assert result.metrics["static_rejections"] == 1
    assert result.metrics["static_rejection_rate"] == 1.0
    assert result.metrics["verification_failures"] == 0
    assert result.cases[0].result is not None
    assert "No safe repair proposal" in (result.cases[0].result.repair_error or "")


def test_repair_suite_counts_applicable_candidates_that_fail_verification(
    tmp_path: Path,
) -> None:
    case = _repair_case(tmp_path / "failed-verification", "failed-verification")
    original = (case / "calculator.py").read_bytes()

    result = evaluate_suite(
        tmp_path,
        client=FailingVerificationClient(),
        model="failing-model",
        max_attempts=1,
        save_report=False,
    )

    assert not result.passed
    assert result.metrics["total_proposals"] == 1
    assert result.metrics["static_rejections"] == 0
    assert result.metrics["verification_failures"] == 1
    assert result.metrics["verification_failure_rate"] == 1.0
    assert (case / "calculator.py").read_bytes() == original


def test_repair_suite_does_not_call_model_for_mismatched_failure_contract(
    tmp_path: Path,
) -> None:
    _case(tmp_path / "mismatch", "mismatch", expected_exit=2)

    result = evaluate_suite(
        tmp_path,
        client=FixedRepairClient(),
        model="unused-model",
        save_report=False,
    )

    assert not result.passed
    assert result.metrics["repair_eligible"] == 0
    assert result.metrics["repair_attempted"] == 0
    assert result.metrics["model_not_attempted"] == 1
    assert result.metrics["total_proposals"] == 0
    assert result.cases[0].result is not None
    assert "model repair was not attempted" in (
        result.cases[0].result.repair_error or ""
    )


def test_committed_repair_suite_schema_matches_v2_envelope(tmp_path: Path) -> None:
    _repair_case(tmp_path / "repair", "repair")
    payload = evaluate_suite(
        tmp_path,
        client=FixedRepairClient(),
        model="fixed-model",
        save_report=False,
    ).to_dict()
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "autofix-evaluation-suite-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema"]["const"] == payload["schema"]
    assert schema["properties"]["version"]["const"] == payload["version"]
    assert set(schema["required"]) == set(payload)
    assert set(schema["properties"]["metrics"]["required"]) == set(
        payload["metrics"]
    )


def test_evaluate_suite_cli_repair_uses_selected_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repair_case(tmp_path / "repair", "repair")
    monkeypatch.setattr(
        "autofix.cli.OllamaClient", lambda _url: FixedRepairClient()
    )

    result = runner.invoke(
        app,
        [
            "evaluate-suite",
            str(tmp_path),
            "--repair",
            "--model",
            "fixed-model",
            "--json",
            "--no-save",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == 2
    assert payload["mode"] == "repair"
    assert payload["model"] == "fixed-model"
    assert payload["autofix_version"]
    assert payload["metrics"]["first_attempt_pass_rate"] == 1.0
