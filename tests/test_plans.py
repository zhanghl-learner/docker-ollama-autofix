from __future__ import annotations

from pathlib import Path

import pytest

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import validate_changes
from autofix.agent.plans import PlanError, load_repair_plan, save_repair_plan
from autofix.builders import DockerConfig
from autofix.models import BuildStep


def test_save_and_load_exact_repair_plan(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    changes = validate_changes(tmp_path, proposal)

    path = save_repair_plan(
        tmp_path, model="qwen3:0.6b", summary=proposal.summary, changes=changes
    )
    plan, loaded = load_repair_plan(path, tmp_path)

    assert plan.model == "qwen3:0.6b"
    planned = plan.to_proposal()
    assert planned.summary == proposal.summary
    assert planned.changes[0].content.replace("\r\n", "\n") == proposal.changes[0].content
    assert loaded[0].replacement.replace("\r\n", "\n") == "value = 2\n"


def test_plan_rejects_project_changed_after_preview(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    path = save_repair_plan(
        tmp_path,
        model="test",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
    )
    source.write_text("value = 3\n", encoding="utf-8")

    with pytest.raises(PlanError, match="changed since preview"):
        load_repair_plan(path, tmp_path)


def test_v3_plan_round_trips_bound_verification_policy(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    steps = (
        BuildStep("Compile", ("python", "-m", "compileall", "-q", ".")),
        BuildStep("Test", ("python", "-m", "pytest")),
    )

    path = save_repair_plan(
        tmp_path,
        model="test",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=steps,
    )
    plan, _ = load_repair_plan(path, tmp_path)

    assert plan.version == 3
    assert plan.verification_steps == steps
    assert plan.verification_runner == "local"
    assert plan.verification_timeout_seconds == 300
    assert plan.docker_config is None
    assert '"version": 3' in path.read_text(encoding="utf-8")


def test_v3_plan_round_trips_docker_policy(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    docker = DockerConfig("python:3.11-slim", 1.5, "768m", 128, False)

    path = save_repair_plan(
        tmp_path,
        model="test",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
        verification_runner="docker",
        docker_config=docker,
    )
    plan, _ = load_repair_plan(path, tmp_path)

    assert plan.verification_runner == "docker"
    assert plan.verification_timeout_seconds == 300
    assert plan.docker_config == docker


def test_loads_legacy_v2_plan_without_runner(tmp_path: Path) -> None:
    import json

    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    path = save_repair_plan(
        tmp_path,
        model="test",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 2
    data["verification"] = {"steps": data["verification"]["steps"]}
    path.write_text(json.dumps(data), encoding="utf-8")

    plan, _ = load_repair_plan(path, tmp_path)

    assert plan.version == 2
    assert plan.verification_runner is None


@pytest.mark.parametrize(
    "verification",
    [
        {},
        {"steps": []},
        {"steps": [{"label": "Test", "argv": []}]},
        {"steps": [{"label": "", "argv": ["python"]}]},
        {
            "steps": [{"label": "Test", "argv": ["python"]}],
            "runner": "local",
            "timeout_seconds": 300,
            "docker": None,
        },
    ],
)
def test_v3_plan_rejects_invalid_verification_metadata(
    tmp_path: Path, verification: object
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    path = save_repair_plan(
        tmp_path,
        model="test",
        summary=proposal.summary,
        changes=validate_changes(tmp_path, proposal),
        verification_steps=(BuildStep("Test", ("python", "-m", "pytest")),),
    )
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data["verification"] = verification
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PlanError, match="verification"):
        load_repair_plan(path, tmp_path)
