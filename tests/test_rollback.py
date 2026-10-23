from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import apply_changes, validate_changes
from autofix.agent.rollback import (
    RollbackError,
    latest_rollback_candidate,
    rollback_report,
    rolled_back_reports,
)


def _write_report(root: Path, original: str, replacement: str) -> Path:
    source = root / "model.py"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "bad repair", (FileChange("model.py", replacement, "add the missing import"),)
    )
    change = validate_changes(root, proposal, allow_destructive_changes=True)[0]
    apply_changes((change,))
    report = root / ".autofix" / "repairs" / "repair.json"
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
    return report


def test_rollback_restores_file_and_keeps_backup(tmp_path: Path) -> None:
    original = """import torch.nn as nn

class Model(nn.Module):
    def forward(self, value):
        return value

print(Model())
"""
    replacement = "import torch\n"
    report = _write_report(tmp_path, original, replacement)

    result = rollback_report(report, tmp_path)

    assert (tmp_path / "model.py").read_text(encoding="utf-8") == original
    assert (result.backup_dir / "model.py").read_text(encoding="utf-8") == replacement
    assert result.restored_paths == (tmp_path / "model.py",)
    assert result.receipt_path.is_file()
    assert report.resolve() in rolled_back_reports(tmp_path)


def test_rollback_refuses_file_that_changed_after_repair(tmp_path: Path) -> None:
    report = _write_report(tmp_path, "value = 1\n", "value = 2\n")
    (tmp_path / "model.py").write_text("value = 3\n", encoding="utf-8")

    with pytest.raises(RollbackError, match="no longer matches"):
        rollback_report(report, tmp_path)


def test_latest_candidate_skips_already_undone_report(tmp_path: Path) -> None:
    report = _write_report(tmp_path, "value = 1\n", "value = 2\n")
    data = json.loads(report.read_text(encoding="utf-8"))
    data.update({"succeeded": True, "applied": True})
    report.write_text(json.dumps(data), encoding="utf-8")

    assert latest_rollback_candidate(tmp_path) == report.resolve()
    rollback_report(report, tmp_path)

    with pytest.raises(RollbackError, match="not-yet-undone"):
        latest_rollback_candidate(tmp_path)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_rollback_restores_original_newline_bytes(tmp_path: Path, newline: str) -> None:
    original = f"value = 1{newline}print(value){newline}"
    replacement = f"value = 2{newline}print(value){newline}"
    source = tmp_path / "model.py"
    source.write_bytes(original.encode("utf-8"))
    proposal = RepairProposal(
        "change value", (FileChange("model.py", replacement, "change value"),)
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

    rollback_report(report, tmp_path)

    assert source.read_bytes() == original.encode("utf-8")
