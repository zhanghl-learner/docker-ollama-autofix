from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.cli import app
from autofix.evaluation_suite import (
    EvaluationSuiteError,
    discover_evaluation_cases,
    evaluate_suite,
)


runner = CliRunner()


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
