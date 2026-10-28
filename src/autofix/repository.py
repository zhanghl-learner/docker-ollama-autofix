"""Read-only Git workspace inspection for future isolated repair policies."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix.runtime.tools import find_executable


class GitInspectionError(RuntimeError):
    """Raised when Git is present but repository metadata cannot be inspected."""


@dataclass(frozen=True)
class GitChange:
    """One entry from Git porcelain status output."""

    status: str
    path: str
    original_path: str | None = None

    @property
    def untracked(self) -> bool:
        """Return whether Git reports this path as untracked."""

        return self.status == "??"

    def to_dict(self) -> dict[str, Any]:
        """Return a stable machine-readable change record."""

        return {
            "status": self.status,
            "path": self.path,
            "original_path": self.original_path,
            "untracked": self.untracked,
        }


@dataclass(frozen=True)
class GitWorkspaceStatus:
    """Read-only identity and cleanliness state for a requested directory."""

    requested_root: Path
    git_available: bool
    repository_root: Path | None = None
    branch: str | None = None
    head_commit: str | None = None
    detached: bool = False
    changes: tuple[GitChange, ...] = ()

    @property
    def is_repository(self) -> bool:
        """Return whether the directory belongs to a Git work tree."""

        return self.repository_root is not None

    @property
    def clean(self) -> bool:
        """Return whether a repository has no tracked or untracked changes."""

        return self.is_repository and not self.changes

    @property
    def state(self) -> str:
        """Return the stable high-level state used by CLI and future policies."""

        if not self.git_available:
            return "git-unavailable"
        if not self.is_repository:
            return "not-repository"
        return "clean" if self.clean else "dirty"

    @property
    def direct_apply_policy(self) -> str:
        """Describe whether a future Git-aware direct apply may proceed."""

        if not self.git_available:
            return "unknown"
        if not self.is_repository:
            return "unversioned"
        if self.head_commit is None:
            return "blocked-unborn"
        return "allowed" if self.clean else "blocked-dirty"

    def to_dict(self) -> dict[str, Any]:
        """Serialize repository identity without mutating or refreshing Git state."""

        return {
            "requested_root": str(self.requested_root),
            "git_available": self.git_available,
            "is_repository": self.is_repository,
            "repository_root": str(self.repository_root) if self.repository_root else None,
            "branch": self.branch,
            "head_commit": self.head_commit,
            "detached": self.detached,
            "clean": self.clean,
            "state": self.state,
            "direct_apply_policy": self.direct_apply_policy,
            "changes": [change.to_dict() for change in self.changes],
        }


def _run_git(git: str, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded, non-shell Git metadata command."""

    try:
        return subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitInspectionError(f"Unable to inspect Git workspace: {exc}") from exc


def _required_output(
    git: str, root: Path, *args: str, operation: str
) -> str:
    """Return lossless stdout or raise a concise inspection error."""

    completed = _run_git(git, root, *args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Git error"
        raise GitInspectionError(f"Unable to {operation}: {detail}")
    # Porcelain status uses a leading space as part of an unstaged status code
    # and NUL as its record delimiter. Trimming either would corrupt the Git
    # protocol, so callers that need scalar output must normalize it themselves.
    return completed.stdout


def _parse_porcelain(output: str) -> tuple[GitChange, ...]:
    """Parse NUL-delimited porcelain v1, including rename/copy source paths."""

    records = output.split("\0")
    changes: list[GitChange] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise GitInspectionError("Git returned malformed porcelain status output")
        status = record[:2]
        path = record[3:]
        original: str | None = None
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise GitInspectionError("Git returned an incomplete rename/copy status entry")
            original = records[index]
            index += 1
        changes.append(GitChange(status, path, original))
    return tuple(changes)


def inspect_git_workspace(path: Path | str) -> GitWorkspaceStatus:
    """Inspect repository identity and worktree changes without modifying Git."""

    requested = Path(path).expanduser().resolve()
    git = find_executable("git")
    if git is None:
        return GitWorkspaceStatus(requested, False)

    top_level = _run_git(git, requested, "rev-parse", "--show-toplevel")
    if top_level.returncode != 0:
        detail = f"{top_level.stderr}\n{top_level.stdout}".lower()
        if "not a git repository" in detail:
            return GitWorkspaceStatus(requested, True)
        message = top_level.stderr.strip() or top_level.stdout.strip() or "unknown Git error"
        raise GitInspectionError(f"Unable to locate Git repository: {message}")

    repository_root = Path(top_level.stdout.strip()).resolve()
    head_result = _run_git(git, repository_root, "rev-parse", "--verify", "HEAD")
    head_commit = head_result.stdout.strip() if head_result.returncode == 0 else None
    branch_result = _run_git(
        git, repository_root, "symbolic-ref", "--quiet", "--short", "HEAD"
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    status_output = _required_output(
        git,
        repository_root,
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        operation="read Git worktree status",
    )
    return GitWorkspaceStatus(
        requested_root=requested,
        git_available=True,
        repository_root=repository_root,
        branch=branch,
        head_commit=head_commit,
        detached=head_commit is not None and branch is None,
        changes=_parse_porcelain(status_output),
    )
