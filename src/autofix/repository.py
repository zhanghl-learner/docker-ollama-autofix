"""Read-only Git workspace inspection for future isolated repair policies."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix.runtime.tools import find_executable


class GitInspectionError(RuntimeError):
    """Raised when Git is present but repository metadata cannot be inspected."""


class GitWorktreeError(RuntimeError):
    """Raised when an isolated Git worktree cannot be created or removed."""


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
    repository_fingerprint: str | None = None
    project_subpath: str | None = None

    @property
    def is_repository(self) -> bool:
        """Return whether the directory belongs to a Git work tree."""

        return self.repository_root is not None

    @property
    def clean(self) -> bool:
        """Return whether a repository has no tracked or untracked changes."""

        return self.is_repository and not self.changes

    @property
    def blocking_changes(self) -> tuple[GitChange, ...]:
        """Return changes that may contain user work and must block repair."""

        prefix = "" if self.project_subpath in {None, "."} else f"{self.project_subpath}/"
        artifact_prefix = f"{prefix}.autofix/"
        return tuple(
            change
            for change in self.changes
            if not (change.untracked and change.path.startswith(artifact_prefix))
        )

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
        return "allowed" if not self.blocking_changes else "blocked-dirty"

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
            "repository_fingerprint": self.repository_fingerprint,
            "project_subpath": self.project_subpath,
            "blocking_changes": [
                change.to_dict() for change in self.blocking_changes
            ],
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass
class DetachedGitWorktree:
    """A temporary detached worktree rooted at an inspected baseline commit."""

    git: str
    repository_root: Path
    workspace_root: Path
    project_root: Path
    baseline_commit: str
    _temporary: tempfile.TemporaryDirectory[str]
    _closed: bool = False

    def close(self) -> None:
        """Remove this worktree's metadata and files without global pruning."""

        if self._closed:
            return
        self._closed = True
        errors: list[str] = []
        try:
            removed = _run_git(
                self.git,
                self.repository_root,
                "worktree",
                "remove",
                "--force",
                str(self.workspace_root),
                timeout_seconds=60,
            )
            if removed.returncode != 0:
                detail = (
                    removed.stderr.strip()
                    or removed.stdout.strip()
                    or "unknown Git error"
                )
                errors.append(f"Git worktree remove failed: {detail}")
        except GitInspectionError as exc:
            errors.append(str(exc))
        try:
            self._temporary.cleanup()
        except OSError as exc:
            errors.append(f"temporary file cleanup failed: {exc}")
        if errors:
            raise GitWorktreeError(
                "Unable to fully clean temporary Git worktree: "
                + "; ".join(errors)
                + ". Inspect 'git worktree list' and prune only the stale AutoFix entry."
            )


def _run_git(
    git: str,
    root: Path,
    *args: str,
    timeout_seconds: float = 10,
) -> subprocess.CompletedProcess[str]:
    """Run a bounded, non-shell Git metadata command."""

    try:
        return subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=timeout_seconds,
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


def _find_git_marker(path: Path) -> Path | None:
    """Find a parent .git directory/file when the Git executable is unavailable."""

    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.exists():
            return marker
    return None


def _repository_fingerprint(git: str, root: Path) -> str:
    """Hash repository lineage without persisting a remote URL or local path."""

    remote = _run_git(git, root, "config", "--get", "remote.origin.url")
    if remote.returncode == 0 and remote.stdout.strip():
        identity = "origin\0" + remote.stdout.strip()
    else:
        roots = _required_output(
            git,
            root,
            "rev-list",
            "--max-parents=0",
            "HEAD",
            operation="read Git root commits",
        )
        identity = "roots\0" + "\n".join(sorted(roots.splitlines()))
    return hashlib.sha256(identity.encode("utf-8", errors="surrogateescape")).hexdigest()


def inspect_git_workspace(path: Path | str) -> GitWorkspaceStatus:
    """Inspect repository identity and worktree changes without modifying Git."""

    requested = Path(path).expanduser().resolve()
    git = find_executable("git")
    if git is None:
        marker = _find_git_marker(requested)
        if marker is not None:
            raise GitInspectionError(
                f"Git metadata exists at {marker}, but the Git executable is unavailable"
            )
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
    try:
        relative_project = requested.relative_to(repository_root)
    except ValueError as exc:
        raise GitInspectionError(
            "Git reported a repository root that does not contain the requested project"
        ) from exc
    return GitWorkspaceStatus(
        requested_root=requested,
        git_available=True,
        repository_root=repository_root,
        branch=branch,
        head_commit=head_commit,
        detached=head_commit is not None and branch is None,
        changes=_parse_porcelain(status_output),
        repository_fingerprint=(
            _repository_fingerprint(git, repository_root) if head_commit else None
        ),
        project_subpath=relative_project.as_posix() if relative_project.parts else ".",
    )


def validate_git_repair_workspace(path: Path | str) -> GitWorkspaceStatus:
    """Return Git state or reject a repository without a safe committed baseline."""

    status = inspect_git_workspace(path)
    if not status.is_repository or status.direct_apply_policy == "allowed":
        return status
    if status.direct_apply_policy == "blocked-dirty":
        changed = ", ".join(change.path for change in status.blocking_changes[:5])
        suffix = " ..." if len(status.blocking_changes) > 5 else ""
        detail = f" Changed paths: {changed}{suffix}." if changed else ""
        raise GitWorktreeError(
            "Git worktree is dirty; AutoFix will not stash, clean, or overwrite "
            f"uncommitted work.{detail} Commit or stash it explicitly, then retry."
        )
    raise GitWorktreeError(
        "Git repository has no baseline commit; create an initial commit before repair."
    )


def create_detached_worktree(status: GitWorkspaceStatus) -> DetachedGitWorktree:
    """Create a detached worktree for a clean repository and requested subpath."""

    if (
        status.direct_apply_policy != "allowed"
        or status.repository_root is None
        or status.head_commit is None
    ):
        raise GitWorktreeError(
            f"Git workspace policy does not allow isolation: {status.direct_apply_policy}"
        )
    git = find_executable("git")
    if git is None:
        raise GitWorktreeError("Git became unavailable before worktree creation")
    try:
        relative_project = status.requested_root.relative_to(status.repository_root)
    except ValueError as exc:
        raise GitWorktreeError("Requested project is outside the Git repository root") from exc

    temporary = tempfile.TemporaryDirectory(prefix="autofix-worktree-")
    workspace_root = Path(temporary.name) / "workspace"
    try:
        created = _run_git(
            git,
            status.repository_root,
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            status.head_commit,
            timeout_seconds=120,
        )
    except GitInspectionError as exc:
        temporary.cleanup()
        raise GitWorktreeError(f"Unable to create temporary Git worktree: {exc}") from exc
    if created.returncode != 0:
        temporary.cleanup()
        detail = created.stderr.strip() or created.stdout.strip() or "unknown Git error"
        raise GitWorktreeError(f"Unable to create temporary Git worktree: {detail}")
    project_root = (workspace_root / relative_project).resolve()
    if not project_root.is_dir():
        handle = DetachedGitWorktree(
            git,
            status.repository_root,
            workspace_root,
            project_root,
            status.head_commit,
            temporary,
        )
        error = GitWorktreeError(
            f"Project subdirectory is missing from baseline commit: {relative_project}"
        )
        try:
            handle.close()
        except GitWorktreeError as cleanup_error:
            error.add_note(f"Worktree cleanup also failed: {cleanup_error}")
        raise error
    return DetachedGitWorktree(
        git,
        status.repository_root,
        workspace_root,
        project_root,
        status.head_commit,
        temporary,
    )
