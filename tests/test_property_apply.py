from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.agent.textio import write_utf8_exact
from autofix.property_apply import (
    ExclusiveProjectFile,
    PropertyApplyPolicyError,
    has_exact_untracked_target,
    load_applied_reviewed_plan,
    publish_json_exclusive,
)
from autofix.repository import GitChange, GitWorkspaceStatus


def _write_plan(project: Path) -> Path:
    source = "value = 2\n"
    write_utf8_exact(project / "module.py", source)
    plan = project / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "changes": [{"path": "module.py", "content": source}],
                "verification": {
                    "runner": "local",
                    "steps": [{"label": "Compile", "argv": ["python", "-m", "compileall", "."]}],
                },
                "diff_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    return plan


def test_load_applied_reviewed_plan_authenticates_content_and_steps(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)

    state = load_applied_reviewed_plan(plan_path, tmp_path)

    assert state.data["diff_sha256"] == "0" * 64
    assert state.steps[0].argv == ("python", "-m", "compileall", ".")


def test_load_applied_reviewed_plan_rejects_source_drift(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    write_utf8_exact(tmp_path / "module.py", "value = 3\n")

    with pytest.raises(PropertyApplyPolicyError, match="not applied exactly"):
        load_applied_reviewed_plan(plan_path, tmp_path)


def test_exclusive_project_file_rolls_back_unretained_content(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "test_generated.py"

    with ExclusiveProjectFile(tmp_path, target, "def test_value():\n    pass\n"):
        assert target.is_file()

    assert not target.exists()
    assert not target.parent.exists()


def test_exclusive_project_file_retains_only_after_explicit_acceptance(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "test_generated.py"

    with ExclusiveProjectFile(
        tmp_path, target, "def test_value():\n    pass\n"
    ) as created:
        created.retain()

    assert target.read_text(encoding="utf-8") == "def test_value():\n    pass\n"


def test_exclusive_project_file_rolls_back_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "test_generated.py"

    with pytest.raises(KeyboardInterrupt):
        with ExclusiveProjectFile(tmp_path, target, "pending\n"):
            raise KeyboardInterrupt

    assert not target.exists()


def test_shared_git_gate_and_receipt_publication_are_exact(tmp_path: Path) -> None:
    baseline = GitWorkspaceStatus(
        tmp_path,
        True,
        repository_root=tmp_path,
        head_commit="a" * 40,
        repository_fingerprint="b" * 64,
        project_subpath=".",
    )
    current = GitWorkspaceStatus(
        tmp_path,
        True,
        repository_root=tmp_path,
        head_commit="a" * 40,
        changes=(GitChange("??", "tests/test_generated.py"),),
        repository_fingerprint="b" * 64,
        project_subpath=".",
    )
    assert has_exact_untracked_target(
        baseline, current, "tests/test_generated.py"
    )

    receipt = tmp_path / "receipt.json"
    publish_json_exclusive(receipt, {"status": "passed"}, label="Test receipt")
    assert json.loads(receipt.read_text(encoding="utf-8")) == {"status": "passed"}
    with pytest.raises(PropertyApplyPolicyError, match="already exists"):
        publish_json_exclusive(receipt, {"status": "changed"}, label="Test receipt")
    assert json.loads(receipt.read_text(encoding="utf-8")) == {"status": "passed"}
