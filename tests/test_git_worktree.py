from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.agent import FileChange, RepairProposal, repair_project
from autofix.agent.patches import UnsafePatchError
from autofix.builders import BuildResult, run_build
from autofix.cli import app
from autofix.models import BuildStep


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
runner = CliRunner()


class FixedClient:
    def __init__(self, replacement: str) -> None:
        self.replacement = replacement
        self.calls = 0

    def propose(self, **_: object) -> RepairProposal:
        self.calls += 1
        return RepairProposal(
            "repair committed value",
            (FileChange("value.py", self.replacement, "match required value"),),
        )


class RaisingClient:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, **_: object) -> RepairProposal:
        self.calls += 1
        raise RuntimeError("model interrupted")


class CancellingClient:
    def propose(self, **_: object) -> RepairProposal:
        raise KeyboardInterrupt()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository_project(tmp_path: Path) -> tuple[Path, Path, tuple[BuildStep, ...]]:
    repository = tmp_path / "repository"
    project = repository / "service"
    project.mkdir(parents=True)
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "AutoFix Tests")
    _git(repository, "config", "user.email", "autofix@example.invalid")
    (repository / ".gitignore").write_text(".autofix/\n", encoding="utf-8")
    (project / "value.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "service/value.py")
    _git(
        repository,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        "broken baseline",
    )
    check = "from value import value; raise SystemExit(0 if value == 2 else 1)"
    steps = (BuildStep("check", (sys.executable, "-c", check)),)
    return repository, project, steps


def _worktree_registry(repository: Path) -> str:
    return _git(repository, "worktree", "list", "--porcelain")


def test_git_preview_runs_entire_build_loop_in_detached_subproject_worktree(
    tmp_path: Path,
) -> None:
    repository, project, steps = _repository_project(tmp_path)
    source = project / "value.py"
    before_registry = _worktree_registry(repository)
    executed_roots: list[Path] = []

    def recording_runner(
        root: Path, build_steps: tuple[BuildStep, ...], **kwargs: object
    ) -> BuildResult:
        executed_roots.append(root)
        return run_build(root, build_steps, **kwargs)

    result = repair_project(
        project,
        steps,
        FixedClient("value = 2\n"),
        model="test",
        apply=False,
        save_report=True,
        build_runner=recording_runner,
    )

    assert result.succeeded and result.isolated
    assert result.workspace_kind == "git-worktree"
    assert result.project_root == project.resolve()
    assert result.repository_root == repository.resolve()
    assert result.baseline_commit == _git(repository, "rev-parse", "HEAD")
    assert len(executed_roots) == 2
    assert all(root != project.resolve() for root in executed_roots)
    assert executed_roots[0].name == "service"
    assert not executed_roots[0].exists()
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert _worktree_registry(repository) == before_registry
    assert _git(repository, "status", "--porcelain") == ""
    assert result.report_path is not None
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["version"] == 2
    assert report["root"] == str(project.resolve())
    assert report["workspace"]["kind"] == "git-worktree"
    assert report["workspace"]["baseline_commit"] == result.baseline_commit
    assert "autofix-worktree-" not in json.dumps(report)


def test_dirty_git_repository_stops_before_build_or_model(tmp_path: Path) -> None:
    repository, project, steps = _repository_project(tmp_path)
    local = repository / "local-notes.txt"
    local.write_text("do not touch\n", encoding="utf-8")
    before_bytes = local.read_bytes()
    before_status = _git(repository, "status", "--porcelain", "-z")
    client = FixedClient("value = 2\n")
    build_calls = 0

    def forbidden_runner(*_: object, **__: object) -> BuildResult:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("build must not run for a dirty Git workspace")

    with pytest.raises(UnsafePatchError, match="will not stash, clean, or overwrite"):
        repair_project(
            project,
            steps,
            client,
            model="test",
            apply=False,
            save_report=False,
            build_runner=forbidden_runner,
        )

    assert build_calls == 0
    assert client.calls == 0
    assert local.read_bytes() == before_bytes
    assert _git(repository, "status", "--porcelain", "-z") == before_status


def test_unborn_git_repository_stops_before_build_or_model(tmp_path: Path) -> None:
    repository = tmp_path / "unborn"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    source = repository / "value.py"
    source.write_text("value = 1\n", encoding="utf-8")
    client = FixedClient("value = 2\n")
    build_calls = 0

    def forbidden_runner(*_: object, **__: object) -> BuildResult:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("build must not run without a baseline commit")

    with pytest.raises(UnsafePatchError, match="no baseline commit"):
        repair_project(
            repository,
            (BuildStep("check", (sys.executable, "value.py")),),
            client,
            model="test",
            apply=False,
            save_report=False,
            build_runner=forbidden_runner,
        )

    assert build_calls == 0
    assert client.calls == 0
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_cli_rejects_dirty_git_before_constructing_ollama_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, project, _ = _repository_project(tmp_path)
    (repository / "local-notes.txt").write_text("do not touch\n", encoding="utf-8")

    def forbidden_client(*_: object, **__: object) -> object:
        raise AssertionError("Ollama client must not be created for a dirty repository")

    monkeypatch.setattr("autofix.cli.OllamaClient", forbidden_client)
    result = runner.invoke(app, ["repair", str(project)])

    assert result.exit_code == 2
    assert "Git worktree is dirty" in result.stdout


def test_failed_git_preview_cleans_worktree_and_keeps_original(tmp_path: Path) -> None:
    repository, project, steps = _repository_project(tmp_path)
    before_registry = _worktree_registry(repository)
    executed_roots: list[Path] = []

    def recording_runner(
        root: Path, build_steps: tuple[BuildStep, ...], **kwargs: object
    ) -> BuildResult:
        executed_roots.append(root)
        return run_build(root, build_steps, **kwargs)

    result = repair_project(
        project,
        steps,
        FixedClient("value = 3\n"),
        model="test",
        apply=False,
        save_report=False,
        build_runner=recording_runner,
    )

    assert not result.succeeded
    assert result.workspace_kind == "git-worktree"
    assert (project / "value.py").read_text(encoding="utf-8") == "value = 1\n"
    assert executed_roots and not executed_roots[0].exists()
    assert _worktree_registry(repository) == before_registry


def test_model_exception_still_removes_git_worktree_registration(tmp_path: Path) -> None:
    repository, project, steps = _repository_project(tmp_path)
    before_registry = _worktree_registry(repository)

    with pytest.raises(RuntimeError, match="model interrupted"):
        repair_project(
            project,
            steps,
            RaisingClient(),
            model="test",
            apply=False,
            save_report=False,
        )

    assert (project / "value.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _worktree_registry(repository) == before_registry


def test_keyboard_interrupt_still_removes_git_worktree_registration(
    tmp_path: Path,
) -> None:
    repository, project, steps = _repository_project(tmp_path)
    before_registry = _worktree_registry(repository)

    with pytest.raises(KeyboardInterrupt):
        repair_project(
            project,
            steps,
            CancellingClient(),
            model="test",
            apply=False,
            save_report=False,
        )

    assert (project / "value.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _worktree_registry(repository) == before_registry


def test_verification_timeout_still_removes_git_worktree_registration(
    tmp_path: Path,
) -> None:
    repository, project, _ = _repository_project(tmp_path)
    before_registry = _worktree_registry(repository)
    timeout_steps = (
        BuildStep(
            "timeout",
            (sys.executable, "-c", "import time; time.sleep(5)"),
        ),
    )

    result = repair_project(
        project,
        timeout_steps,
        FixedClient("value = 2\n"),
        model="test",
        apply=False,
        timeout_seconds=0.05,
        save_report=False,
    )

    assert not result.succeeded
    assert result.final_build.steps[-1].timed_out is True
    assert (project / "value.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _worktree_registry(repository) == before_registry


def test_verified_git_apply_writes_only_final_target_after_worktree_passes(
    tmp_path: Path,
) -> None:
    repository, project, steps = _repository_project(tmp_path)
    before_registry = _worktree_registry(repository)

    result = repair_project(
        project,
        steps,
        FixedClient("value = 2\n"),
        model="test",
        apply=True,
        isolate=True,
        save_report=False,
    )

    assert result.succeeded and result.applied
    assert result.workspace_kind == "git-worktree"
    assert (project / "value.py").read_text(encoding="utf-8") == "value = 2\n"
    assert _git(repository, "status", "--porcelain") == "M service/value.py"
    assert _worktree_registry(repository) == before_registry
