"""Shared safety primitives for explicit property-test application."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix.agent.textio import read_utf8_exact
from autofix.models import BuildStep
from autofix.repository import GitWorkspaceStatus


class PropertyApplyPolicyError(RuntimeError):
    """Raised when shared property-application evidence or I/O is unsafe."""


@dataclass(frozen=True)
class ReviewedPlanState:
    """Authenticated reviewed changes and their fixed local verification matrix."""

    data: dict[str, Any]
    steps: tuple[BuildStep, ...]


def load_applied_reviewed_plan(
    plan_path: Path,
    project_root: Path,
    *,
    label: str = "Repair plan",
) -> ReviewedPlanState:
    """Require every reviewed plan change to match the current project exactly."""

    if not plan_path.is_file() or plan_path.is_symlink():
        raise PropertyApplyPolicyError(f"{label} must be a regular file")
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyApplyPolicyError(f"{label} is unavailable or malformed") from exc
    raw_changes = data.get("changes") if isinstance(data, dict) else None
    verification = data.get("verification") if isinstance(data, dict) else None
    if not isinstance(raw_changes, list) or not raw_changes:
        raise PropertyApplyPolicyError(f"{label} has no reviewed changes")
    for change in raw_changes:
        if (
            not isinstance(change, dict)
            or not isinstance(change.get("path"), str)
            or not isinstance(change.get("content"), str)
        ):
            raise PropertyApplyPolicyError(
                f"{label} contains invalid reviewed changes"
            )
        relative = Path(change["path"])
        current = (project_root / relative).resolve()
        try:
            current.relative_to(project_root)
        except ValueError as exc:
            raise PropertyApplyPolicyError(
                f"{label} path escapes the project root"
            ) from exc
        try:
            matches = (
                current.is_file()
                and not current.is_symlink()
                and read_utf8_exact(current) == change["content"]
            )
        except (OSError, UnicodeError) as exc:
            raise PropertyApplyPolicyError(
                f"Unable to authenticate reviewed file: {relative.as_posix()}"
            ) from exc
        if not matches:
            raise PropertyApplyPolicyError(
                f"Reviewed repair is not applied exactly: {relative.as_posix()}"
            )
    if not isinstance(verification, dict) or verification.get("runner") != "local":
        raise PropertyApplyPolicyError(
            "Property apply requires a local plan verification policy"
        )
    raw_steps = verification.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PropertyApplyPolicyError(f"{label} has no verification steps")
    steps: list[BuildStep] = []
    for raw in raw_steps:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"label", "argv"}
            or not isinstance(raw["label"], str)
            or not isinstance(raw["argv"], list)
            or not raw["argv"]
            or not all(isinstance(item, str) and item for item in raw["argv"])
        ):
            raise PropertyApplyPolicyError(
                f"{label} contains invalid verification steps"
            )
        steps.append(BuildStep(raw["label"], tuple(raw["argv"])))
    return ReviewedPlanState(data, tuple(steps))


def has_exact_untracked_target(
    baseline: GitWorkspaceStatus,
    current: GitWorkspaceStatus,
    target_path: str,
) -> bool:
    """Return whether Git shows only the requested new untracked project file."""

    prefix = "" if current.project_subpath in {None, "."} else f"{current.project_subpath}/"
    expected_change = f"{prefix}{target_path}"
    blocking = current.blocking_changes
    return (
        current.head_commit == baseline.head_commit
        and current.repository_fingerprint == baseline.repository_fingerprint
        and len(blocking) == 1
        and blocking[0].untracked
        and blocking[0].path == expected_change
    )


class ExclusiveProjectFile:
    """Create one project file exclusively and remove it unless explicitly retained."""

    def __init__(self, project_root: Path, destination: Path, content: str) -> None:
        self.project_root = project_root
        self.destination = destination
        self.content = content
        self.created_directories: list[Path] = []
        self.retained = False
        self.created = False

    def __enter__(self) -> ExclusiveProjectFile:
        """Create missing parents and the destination without allowing overwrite."""

        try:
            self.destination.resolve().relative_to(self.project_root)
        except ValueError as exc:
            raise PropertyApplyPolicyError(
                "Property target escapes the project root"
            ) from exc
        parent = self.destination.parent
        while parent != self.project_root and not parent.exists():
            self.created_directories.append(parent)
            parent = parent.parent
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.destination.open("x", encoding="utf-8", newline="") as stream:
                self.created = True
                stream.write(self.content)
        except BaseException as exc:
            cleanup_error = self._rollback()
            if cleanup_error is not None:
                exc.add_note(f"Property target rollback also failed: {cleanup_error}")
            raise
        return self

    def retain(self) -> None:
        """Keep the created file after all caller-controlled checks succeed."""

        self.retained = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Rollback on exceptions or when the caller did not explicitly retain."""

        if exc_type is not None or not self.retained:
            cleanup_error = self._rollback()
            if cleanup_error is not None:
                if isinstance(exc, BaseException):
                    exc.add_note(
                        f"Property target rollback also failed: {cleanup_error}"
                    )
                else:
                    raise PropertyApplyPolicyError(
                        f"Unable to remove rejected property target: {cleanup_error}"
                    ) from cleanup_error

    def _rollback(self) -> OSError | None:
        unlink_error: OSError | None = None
        if self.created:
            try:
                self.destination.unlink(missing_ok=True)
            except OSError as exc:
                unlink_error = exc
            else:
                self.created = False
        for directory in self.created_directories:
            try:
                directory.rmdir()
            except OSError:
                break
        return unlink_error


def publish_json_exclusive(
    destination: Path,
    payload: dict[str, Any],
    *,
    label: str,
) -> None:
    """Atomically publish complete JSON evidence without replacing any file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PropertyApplyPolicyError(f"{label} already exists: {destination}")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        os.link(temporary_path, destination)
        temporary_path.unlink()
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise PropertyApplyPolicyError(f"Unable to publish {label}: {exc}") from exc
