from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import validate_changes, validated_diff_sha256
from autofix.agent.plans import (
    PlanError,
    PlanProvenance,
    load_repair_plan,
    save_repair_plan,
)
from autofix.builders import DockerConfig
from autofix.models import BuildStep
from autofix.repository import validate_git_repair_workspace


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _git_project(tmp_path: Path, name: str = "repository") -> tuple[Path, Path]:
    repository = tmp_path / name
    project = repository / "service"
    project.mkdir(parents=True)
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "AutoFix Tests")
    _git(repository, "config", "user.email", "autofix@example.invalid")
    (repository / ".gitignore").write_text(".autofix/\n", encoding="utf-8")
    (repository / "identity.txt").write_text(name + "\n", encoding="utf-8")
    (project / "main.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        f"baseline {name}",
    )
    return repository, project


def _git_provenance(project: Path) -> PlanProvenance:
    status = validate_git_repair_workspace(project)
    return PlanProvenance(
        "git",
        status.repository_fingerprint,
        status.head_commit,
        status.project_subpath,
    )


def _v4_plan(project: Path) -> tuple[Path, tuple]:
    proposal = RepairProposal(
        "correct value", (FileChange("main.py", "value = 2\n", "fix value"),)
    )
    changes = validate_changes(project, proposal)
    path = save_repair_plan(
        project,
        model="test",
        summary=proposal.summary,
        changes=changes,
        verification_steps=(BuildStep("Test", ("python", "main.py")),),
        provenance=_git_provenance(project),
    )
    return path, changes


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


def test_final_diff_digest_is_stable_across_change_order(tmp_path: Path) -> None:
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = 1\n", encoding="utf-8")
    forward = validate_changes(
        tmp_path,
        RepairProposal(
            "update both",
            (
                FileChange("a.py", "a = 2\n", "update a"),
                FileChange("b.py", "b = 2\n", "update b"),
            ),
        ),
    )
    reverse = validate_changes(
        tmp_path,
        RepairProposal(
            "update both",
            (
                FileChange("b.py", "b = 2\n", "update b"),
                FileChange("a.py", "a = 2\n", "update a"),
            ),
        ),
    )

    assert validated_diff_sha256(forward) == validated_diff_sha256(reverse)


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


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_plan_binds_git_provenance_and_final_diff(tmp_path: Path) -> None:
    repository, project = _git_project(tmp_path)
    secret_remote = "https://user:secret@example.invalid/owner/repository.git"
    _git(repository, "remote", "add", "origin", secret_remote)
    path, changes = _v4_plan(project)

    plan, loaded = load_repair_plan(path, project)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert plan.version == 4
    assert plan.provenance == _git_provenance(project)
    assert plan.diff_sha256 == validated_diff_sha256(changes)
    assert validated_diff_sha256(loaded) == plan.diff_sha256
    assert data["provenance"]["project_subpath"] == "service"
    assert len(data["provenance"]["repository_fingerprint"]) == 64
    assert data["provenance"]["baseline_commit"] == _git(repository, "rev-parse", "HEAD")
    assert secret_remote not in path.read_text(encoding="utf-8")
    assert "secret" not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_plan_rejects_advanced_head_even_when_target_is_unchanged(
    tmp_path: Path,
) -> None:
    repository, project = _git_project(tmp_path)
    path, _ = _v4_plan(project)
    (repository / "unrelated.txt").write_text("new commit\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(
        repository,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        "advance head",
    )

    with pytest.raises(PlanError, match="HEAD differs"):
        load_repair_plan(path, project)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_plan_rejects_different_repository_identity(tmp_path: Path) -> None:
    _, first_project = _git_project(tmp_path, "first")
    path, _ = _v4_plan(first_project)
    _, second_project = _git_project(tmp_path, "second")

    with pytest.raises(PlanError, match="repository identity differs"):
        load_repair_plan(path, second_project)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_plan_rejects_dirty_workspace_before_apply(tmp_path: Path) -> None:
    repository, project = _git_project(tmp_path)
    path, _ = _v4_plan(project)
    (repository / "local.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(PlanError, match="Git worktree is dirty"):
        load_repair_plan(path, project)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_plan_rejects_tampered_diff_digest(tmp_path: Path) -> None:
    _, project = _git_project(tmp_path)
    path, _ = _v4_plan(project)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["diff_sha256"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PlanError, match="diff SHA-256 does not match"):
        load_repair_plan(path, project)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_v4_unversioned_plan_rejects_later_git_repository(tmp_path: Path) -> None:
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
        verification_steps=(BuildStep("Test", ("python", "main.py")),),
        provenance=PlanProvenance("unversioned"),
    )
    _git(tmp_path, "init", "--quiet")

    with pytest.raises(PlanError, match="Unversioned repair plan"):
        load_repair_plan(path, tmp_path)


def test_loads_legacy_v2_plan_without_runner(tmp_path: Path) -> None:
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
    data = json.loads(path.read_text(encoding="utf-8"))
    data["verification"] = verification
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PlanError, match="verification"):
        load_repair_plan(path, tmp_path)
