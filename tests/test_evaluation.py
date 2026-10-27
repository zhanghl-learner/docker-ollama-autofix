"""Tests for reproducible, non-mutating benchmark evaluation."""

from __future__ import annotations

from pathlib import Path

import pytest

from autofix.agent.ollama import FileEdit, RepairProposal
from autofix.evaluation import (
    EvaluationError,
    evaluate_project,
    load_evaluation_manifest,
)


class FixedClient:
    """Return a deterministic minimal edit for the repair evaluation test."""

    def propose(self, **_: object) -> RepairProposal:
        """Replace the known incorrect return expression."""

        return RepairProposal(
            "fix addition",
            edits=(
                FileEdit(
                    "calculator.py",
                    2,
                    1,
                    "    return left - right",
                    "    return left + right",
                    "add instead of subtracting",
                ),
            ),
        )


class UnsafeClient:
    """Return a stale edit that the source-preserving validator rejects."""

    def propose(self, **_: object) -> RepairProposal:
        """Produce an edit whose old text is absent from the target file."""

        return RepairProposal(
            "unsafe edit",
            edits=(FileEdit("calculator.py", 1, 1, "missing", "replacement", "bad"),),
        )


class UnexpectedClient:
    """Fail the test if a mismatched baseline reaches the model boundary."""

    def propose(self, **_: object) -> RepairProposal:
        """Signal that preflight failed to stop an invalid benchmark."""

        raise AssertionError("model must not be called for a mismatched baseline")


def _case(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (tmp_path / "check.py").write_text(
        "from calculator import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (tmp_path / "autofix-eval.yml").write_text(
        """version: 1
name: python-addition
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
tags: [single-file]
""",
        encoding="utf-8",
    )


def test_baseline_confirms_failure_and_saves_report(tmp_path: Path) -> None:
    _case(tmp_path)
    manifest = load_evaluation_manifest(tmp_path)

    result = evaluate_project(tmp_path, manifest)

    assert result.passed
    assert result.initial_failure_confirmed
    assert result.failure_contract_matched
    assert result.mode == "baseline"
    assert result.report_path is not None and result.report_path.is_file()


def test_repair_is_scored_without_mutating_original(tmp_path: Path) -> None:
    _case(tmp_path)
    original = (tmp_path / "calculator.py").read_bytes()

    result = evaluate_project(
        tmp_path,
        load_evaluation_manifest(tmp_path),
        client=FixedClient(),
        model="fixed-model",
    )

    assert result.passed
    assert result.changed_paths == ("calculator.py",)
    assert result.model == "fixed-model"
    assert result.runner == "local"
    assert result.unexpected_change_paths == ()
    assert result.missing_required_paths == ()
    assert (tmp_path / "calculator.py").read_bytes() == original


def test_manifest_rejects_unsafe_change_path(tmp_path: Path) -> None:
    _case(tmp_path)
    manifest = tmp_path / "autofix-eval.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "  - calculator.py\nrequired_change_paths:",
            "  - ../calculator.py\nrequired_change_paths:",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="Unsafe path"):
        load_evaluation_manifest(tmp_path)


def test_rejected_repair_is_saved_as_failed_evaluation(tmp_path: Path) -> None:
    _case(tmp_path)

    result = evaluate_project(
        tmp_path,
        load_evaluation_manifest(tmp_path),
        client=UnsafeClient(),
        model="unsafe-model",
        max_attempts=1,
    )

    assert not result.passed
    assert result.initial_failure_confirmed
    assert result.repair_error is not None
    assert "No safe repair proposal" in result.repair_error
    assert result.proposal_count == 1
    assert result.rejection_count == 1
    assert result.repair_report_path is not None
    assert result.report_path is not None and result.report_path.is_file()


def test_failure_signature_mismatch_stops_before_model(tmp_path: Path) -> None:
    _case(tmp_path)
    manifest_path = tmp_path / "autofix-eval.yml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "  failed_step: 1\n",
            "  failed_step: 1\n  output_contains: [expected business failure]\n",
        ),
        encoding="utf-8",
    )

    result = evaluate_project(
        tmp_path,
        load_evaluation_manifest(tmp_path),
        client=UnexpectedClient(),
        model="unused-model",
    )

    assert not result.passed
    assert result.initial_failure_confirmed
    assert not result.failure_contract_matched
    assert result.proposal_count == 0
    assert "model repair was not attempted" in (result.repair_error or "")
    assert "required output is missing" in result.failure_contract_mismatches[0]
