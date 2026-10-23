from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from autofix.agent import (
    FileChange,
    FileEdit,
    RepairEnvironmentError,
    RepairProposal,
    repair_project,
)
from autofix.agent.patches import UnsafePatchError
from autofix.builders import BuildResult, StepResult
from autofix.models import BuildStep


class FakeClient:
    def __init__(self, proposal: RepairProposal) -> None:
        self.proposal = proposal
        self.calls = 0

    def propose(self, **_: object) -> RepairProposal:
        self.calls += 1
        return self.proposal


class FailingSecondClient:
    def __init__(self, proposal: RepairProposal) -> None:
        self.proposal = proposal
        self.calls = 0

    def propose(self, **_: object) -> RepairProposal:
        self.calls += 1
        if self.calls == 1:
            return self.proposal
        raise RuntimeError("model failed")


class SequenceClient:
    def __init__(self, proposals: list[RepairProposal]) -> None:
        self.proposals = proposals
        self.calls: list[dict[str, object]] = []

    def propose(self, **kwargs: object) -> RepairProposal:
        self.calls.append(kwargs)
        return self.proposals[len(self.calls) - 1]


def _project(tmp_path: Path) -> tuple[Path, tuple[BuildStep, ...]]:
    source = tmp_path / "value.py"
    source.write_text("value = 1\n", encoding="utf-8")
    check = "from value import value; raise SystemExit(0 if value == 2 else 1)"
    return source, (BuildStep("check", (sys.executable, "-c", check)),)


def test_preview_does_not_modify_file(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(RepairProposal("fix", (FileChange("value.py", "value = 2\n", "correct value"),)))

    result = repair_project(tmp_path, steps, client, model="test", apply=False)

    assert result.succeeded
    assert not result.applied
    assert result.isolated
    assert len(result.changes) == 1
    assert result.changes[0].replacement.replace("\r\n", "\n") == "value = 2\n"
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_preview_retries_failed_build_and_returns_only_verified_final_diff(
    tmp_path: Path,
) -> None:
    source, steps = _project(tmp_path)
    client = SequenceClient(
        [
            RepairProposal("wrong", (FileChange("value.py", "value = 3\n", "guess"),)),
            RepairProposal("fix", (FileChange("value.py", "value = 2\n", "correct"),)),
        ]
    )

    result = repair_project(
        tmp_path, steps, client, model="test", apply=False, max_attempts=2
    )

    assert result.succeeded
    assert not result.applied
    assert len(result.proposals) == 2
    assert len(result.changes) == 1
    assert result.changes[0].original.replace("\r\n", "\n") == "value = 1\n"
    assert result.changes[0].replacement.replace("\r\n", "\n") == "value = 2\n"
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_preview_returns_no_plan_changes_when_verification_fails(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(
        RepairProposal("wrong", (FileChange("value.py", "value = 3\n", "guess"),))
    )

    result = repair_project(tmp_path, steps, client, model="test", apply=False)

    assert not result.succeeded
    assert result.changes == ()
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_applies_and_verifies_successful_repair(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(RepairProposal("fix", (FileChange("value.py", "value = 2\n", "correct value"),)))

    result = repair_project(tmp_path, steps, client, model="test", apply=True)

    assert result.succeeded
    assert result.applied
    assert source.read_text(encoding="utf-8") == "value = 2\n"
    assert result.report_path is not None and result.report_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["succeeded"] is True
    assert report["changes"][0]["path"] == "value.py"


def test_import_only_model_output_is_merged_and_verified(tmp_path: Path) -> None:
    source = tmp_path / "program.py"
    original = "result = math.sqrt(9)\nassert result == 3\n"
    source.write_text(original, encoding="utf-8")
    steps = (BuildStep("run", (sys.executable, "program.py")),)
    client = FakeClient(
        RepairProposal("add import", (FileChange("program.py", "import math\n", "import math"),))
    )

    result = repair_project(
        tmp_path, steps, client, model="test", apply=True, isolate=True
    )

    assert result.succeeded
    repaired = source.read_text(encoding="utf-8")
    assert repaired.startswith("import math\n\n")
    assert original in repaired


def test_structured_line_edit_is_applied_and_verified(tmp_path: Path) -> None:
    source = tmp_path / "program.py"
    original = "result = math.sqrt(9)\nassert result == 3\n"
    source.write_text(original, encoding="utf-8")
    steps = (BuildStep("run", (sys.executable, "program.py")),)
    client = FakeClient(
        RepairProposal(
            "add import",
            edits=(FileEdit("program.py", 1, 0, "", "import math\n", "import math"),),
        )
    )

    result = repair_project(
        tmp_path, steps, client, model="test", apply=True, isolate=True
    )

    assert result.succeeded
    assert source.read_text(encoding="utf-8") == "import math\n" + original


def test_rolls_back_failed_repair(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(RepairProposal("wrong", (FileChange("value.py", "value = 3\n", "guess"),)))

    result = repair_project(tmp_path, steps, client, model="test", apply=True)

    assert not result.succeeded
    assert result.rolled_back
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_missing_dependency_stops_before_model_or_code_change(tmp_path: Path) -> None:
    source = tmp_path / "program.py"
    original = "import package_that_does_not_exist_autofix\nprint('unreachable')\n"
    source.write_text(original, encoding="utf-8")
    client = FakeClient(
        RepairProposal("should not run", (FileChange("program.py", "pass\n", "bad"),))
    )

    with pytest.raises(RepairEnvironmentError, match="Missing Python dependency") as caught:
        repair_project(
            tmp_path,
            (BuildStep("run", (sys.executable, "program.py")),),
            client,
            model="test",
            apply=True,
        )

    assert client.calls == 0
    assert source.read_text(encoding="utf-8") == original
    report_path = getattr(caught.value, "report_path", None)
    assert report_path is not None and report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["proposals"] == []
    assert "Code repair was not attempted" in report["error"]


def test_multilanguage_environment_failure_stops_before_model(tmp_path: Path) -> None:
    source = tmp_path / "index.js"
    original = "const left = 1;\n"
    source.write_text(original, encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"left-pad":"1.3.0"}}\n', encoding="utf-8"
    )
    client = FakeClient(
        RepairProposal("should not run", (FileChange("index.js", "", "bad"),))
    )

    def missing_node_dependency(root: Path, steps: object, **_: object) -> BuildResult:
        return BuildResult(
            root,
            (
                StepResult(
                    "Node test",
                    ("node", "index.js"),
                    1,
                    "",
                    "Error: Cannot find module 'left-pad'",
                    0.1,
                ),
            ),
            "2026-10-23T00:00:00+00:00",
        )

    with pytest.raises(RepairEnvironmentError, match="Node.js dependency") as caught:
        repair_project(
            tmp_path,
            (BuildStep("Node test", ("node", "index.js")),),
            client,
            model="test",
            apply=True,
            build_runner=missing_node_dependency,
        )

    assert client.calls == 0
    assert source.read_text(encoding="utf-8") == original
    report_path = getattr(caught.value, "report_path", None)
    assert report_path is not None and report_path.is_file()


def test_rolls_back_when_later_model_attempt_raises(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    proposal = RepairProposal("wrong", (FileChange("value.py", "value = 3\n", "guess"),))
    client = FailingSecondClient(proposal)

    try:
        repair_project(tmp_path, steps, client, model="test", apply=True, max_attempts=2)
    except RuntimeError as exc:
        assert str(exc) == "model failed"
    else:
        raise AssertionError("Expected the second model call to fail")

    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_retries_rejected_test_change_then_repairs_source(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    test_file = tmp_path / "test_value.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    client = SequenceClient(
        [
            RepairProposal(
                "wrong target",
                (FileChange("test_value.py", "assert False\n", "change test"),),
            ),
            RepairProposal(
                "fix source",
                (FileChange("value.py", "value = 2\n", "correct value"),),
            ),
        ]
    )

    result = repair_project(
        tmp_path, steps, client, model="test", apply=True, max_attempts=2
    )

    assert result.succeeded
    assert len(client.calls) == 2
    assert "protected by default" in str(client.calls[1]["previous_attempt"])
    assert result.rejections[0].attempt == 1
    assert test_file.read_text(encoding="utf-8") == "assert True\n"
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_isolated_success_commits_only_after_verification(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(
        RepairProposal("fix", (FileChange("value.py", "value = 2\n", "correct value"),))
    )
    observed_original: list[str] = []

    def runner(root: Path, build_steps: tuple[BuildStep, ...], **kwargs: object):
        observed_original.append(source.read_text(encoding="utf-8"))
        from autofix.builders import run_build

        return run_build(root, build_steps, **kwargs)

    result = repair_project(
        tmp_path,
        steps,
        client,
        model="test",
        apply=True,
        isolate=True,
        build_runner=runner,
    )

    assert result.succeeded and result.isolated
    assert observed_original == ["value = 1\n", "value = 1\n"]
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_isolated_failure_never_changes_original(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(
        RepairProposal("wrong", (FileChange("value.py", "value = 3\n", "guess"),))
    )

    result = repair_project(
        tmp_path, steps, client, model="test", apply=True, isolate=True
    )

    assert not result.succeeded
    assert not result.applied
    assert not result.rolled_back
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_isolated_commit_refuses_concurrent_source_change(tmp_path: Path) -> None:
    source, steps = _project(tmp_path)
    client = FakeClient(
        RepairProposal("fix", (FileChange("value.py", "value = 2\n", "correct value"),))
    )
    calls = 0

    def runner(root: Path, build_steps: tuple[BuildStep, ...], **kwargs: object):
        nonlocal calls
        calls += 1
        from autofix.builders import run_build

        result = run_build(root, build_steps, **kwargs)
        if calls == 2:
            source.write_text("value = 99\n", encoding="utf-8")
        return result

    with pytest.raises(UnsafePatchError, match="changed during isolated repair"):
        repair_project(
            tmp_path,
            steps,
            client,
            model="test",
            apply=True,
            isolate=True,
            build_runner=runner,
        )

    assert source.read_text(encoding="utf-8") == "value = 99\n"

