from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.cli import app
from autofix.repository import GitInspectionError, inspect_git_workspace


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _clean_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "AutoFix Tests")
    _git(root, "config", "user.email", "autofix@example.invalid")
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "tracked.py")
    _git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify", "-m", "baseline")
    return root


def test_non_repository_is_reported_without_error(tmp_path: Path) -> None:
    status = inspect_git_workspace(tmp_path)

    assert status.git_available is True
    assert status.is_repository is False
    assert status.state == "not-repository"
    assert status.direct_apply_policy == "unversioned"


def test_git_marker_without_executable_is_not_treated_as_unversioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repository" / "service"
    project.mkdir(parents=True)
    (tmp_path / "repository" / ".git").mkdir()
    monkeypatch.setattr("autofix.repository.find_executable", lambda _: None)

    with pytest.raises(GitInspectionError, match="Git executable is unavailable"):
        inspect_git_workspace(project)


def test_clean_repository_records_branch_and_baseline_commit(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)

    status = inspect_git_workspace(root)

    assert status.clean is True
    assert status.branch in {"main", "master"}
    assert status.head_commit == _git(root, "rev-parse", "HEAD")
    assert status.repository_fingerprint is not None
    assert len(status.repository_fingerprint) == 64
    assert status.project_subpath == "."
    assert status.direct_apply_policy == "allowed"
    assert status.changes == ()


def test_unborn_repository_has_no_safe_baseline_commit(tmp_path: Path) -> None:
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "--quiet")

    status = inspect_git_workspace(root)

    assert status.clean is True
    assert status.head_commit is None
    assert status.detached is False
    assert status.direct_apply_policy == "blocked-unborn"


def test_dirty_repository_distinguishes_tracked_and_untracked_paths(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (root / "new.py").write_text("new = True\n", encoding="utf-8")

    status = inspect_git_workspace(root)

    assert status.state == "dirty"
    assert status.direct_apply_policy == "blocked-dirty"
    assert {(change.status, change.path) for change in status.changes} == {
        (" M", "tracked.py"),
        ("??", "new.py"),
    }


def test_untracked_project_autofix_artifacts_do_not_block_repair_policy(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)
    report = root / ".autofix" / "runs" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}\n", encoding="utf-8")

    status = inspect_git_workspace(root)

    assert status.state == "dirty"
    assert status.clean is False
    assert status.blocking_changes == ()
    assert status.direct_apply_policy == "allowed"


def test_detached_head_retains_commit_identity(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    _git(root, "checkout", "--quiet", "--detach", "HEAD")

    status = inspect_git_workspace(root)

    assert status.clean is True
    assert status.detached is True
    assert status.branch is None
    assert status.head_commit == _git(root, "rev-parse", "HEAD")


def test_rename_status_preserves_original_and_destination_paths(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    _git(root, "mv", "tracked.py", "renamed.py")

    status = inspect_git_workspace(root)

    assert len(status.changes) == 1
    assert status.changes[0].status == "R "
    assert status.changes[0].path == "renamed.py"
    assert status.changes[0].original_path == "tracked.py"


def test_git_status_cli_json_and_clean_gate(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    clean = runner.invoke(app, ["git-status", str(root), "--json", "--require-clean"])

    assert clean.exit_code == 0, clean.stdout
    assert json.loads(clean.stdout)["state"] == "clean"

    (root / "untracked.txt").write_text("local work\n", encoding="utf-8")
    dirty = runner.invoke(app, ["git-status", str(root), "--json", "--require-clean"])

    assert dirty.exit_code == 1
    assert json.loads(dirty.stdout)["direct_apply_policy"] == "blocked-dirty"
