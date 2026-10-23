from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from autofix.agent.context import collect_source_context, format_failure
from autofix.agent.diagnostics import RepairEnvironmentError, environment_failure
from autofix.agent.ollama import RepairProposal
from autofix.agent.patches import (
    UnsafePatchError,
    ValidatedChange,
    apply_changes,
    restore_files,
    validate_changes,
)
from autofix.agent.textio import read_utf8_exact
from autofix.builders import BuildResult, run_build
from autofix.models import BuildStep


WORKSPACE_IGNORED_PARTS = {
    ".autofix",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}


def _workspace_ignore(directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    ignored = {name for name in names if name in WORKSPACE_IGNORED_PARTS}
    ignored.update(name for name in names if (base / name).is_symlink())
    return ignored


class RepairClient(Protocol):
    def propose(
        self,
        *,
        model: str,
        failure: str,
        files: dict[str, str],
        previous_attempt: str | None = None,
        allow_test_changes: bool = False,
    ) -> RepairProposal: ...


@dataclass(frozen=True)
class RepairResult:
    initial_build: BuildResult
    final_build: BuildResult
    proposals: tuple[RepairProposal, ...]
    changes: tuple[ValidatedChange, ...]
    applied: bool
    rolled_back: bool
    isolated: bool = False
    rejections: tuple["ProposalRejection", ...] = ()
    report_path: Path | None = None

    @property
    def succeeded(self) -> bool:
        return self.final_build.succeeded

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "type": "repair",
            "root": str(self.initial_build.root),
            "succeeded": self.succeeded,
            "applied": self.applied,
            "rolled_back": self.rolled_back,
            "isolated": self.isolated,
            "initial_build": self.initial_build.to_dict(),
            "final_build": self.final_build.to_dict(),
            "proposals": [
                {
                    "summary": proposal.summary,
                    "changes": [
                        {
                            "path": change.path,
                            "reason": change.reason,
                            "content_sha256": hashlib.sha256(
                                change.content.encode("utf-8")
                            ).hexdigest(),
                        }
                        for change in proposal.changes
                    ],
                    "edits": [
                        {
                            "path": edit.path,
                            "start_line": edit.start_line,
                            "delete_lines": edit.delete_lines,
                            "old_text_sha256": hashlib.sha256(
                                edit.old_text.encode("utf-8")
                            ).hexdigest(),
                            "new_text_sha256": hashlib.sha256(
                                edit.new_text.encode("utf-8")
                            ).hexdigest(),
                            "reason": edit.reason,
                        }
                        for edit in proposal.edits
                    ],
                }
                for proposal in self.proposals
            ],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
            "changes": [
                {
                    "path": change.relative_path,
                    "reason": change.reason,
                    "diff": change.diff,
                }
                for change in self.changes
            ],
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class ProposalRejection:
    attempt: int
    summary: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "summary": self.summary, "reason": self.reason}


def _save_repair_report(root: Path, result: RepairResult, *, error: str | None = None) -> Path:
    report_dir = root / ".autofix" / "repairs"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = report_dir / f"{stamp}.json"
    data = result.to_dict()
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["error"] = error
    data["report_path"] = str(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def repair_project(
    root: Path,
    steps: tuple[BuildStep, ...],
    client: RepairClient,
    *,
    model: str,
    max_attempts: int = 1,
    timeout_seconds: float = 300,
    apply: bool = False,
    keep_failed: bool = False,
    allow_test_changes: bool = False,
    allow_destructive_changes: bool = False,
    isolate: bool = False,
    save_report: bool = True,
    build_runner: Callable[..., BuildResult] = run_build,
) -> RepairResult:
    """Build, request bounded repairs, validate them, and rebuild."""

    root = root.resolve()
    initial = build_runner(root, steps, timeout_seconds=timeout_seconds)
    if initial.succeeded:
        result = RepairResult(initial, initial, (), (), False, False, isolate)
        if save_report:
            result = replace(result, report_path=_save_repair_report(root, result))
        return result
    environment_issue = environment_failure(initial)
    if environment_issue:
        result = RepairResult(initial, initial, (), (), False, False, isolate)
        if save_report:
            result = replace(
                result,
                report_path=_save_repair_report(root, result, error=environment_issue),
            )
        error = RepairEnvironmentError(environment_issue)
        setattr(error, "report_path", result.report_path)
        raise error

    current = initial
    proposals: list[RepairProposal] = []
    all_changes: list[ValidatedChange] = []
    backups: dict[Path, bytes] = {}
    previous_attempt: str | None = None
    last_rejection: UnsafePatchError | None = None
    rejections: list[ProposalRejection] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    work_root = root
    original_files: dict[str, bytes] = {}
    using_temporary = isolate or not apply
    if using_temporary:
        temporary = tempfile.TemporaryDirectory(prefix="autofix-repair-")
        work_root = Path(temporary.name) / "workspace"
        shutil.copytree(root, work_root, ignore=_workspace_ignore)

    def cleanup() -> None:
        if temporary is not None:
            temporary.cleanup()

    def finish(
        *,
        applied: bool,
        rolled_back: bool,
        error: str | None = None,
        result_changes: tuple[ValidatedChange, ...] | None = None,
    ) -> RepairResult:
        result = RepairResult(
            initial,
            current,
            tuple(proposals),
            result_changes if result_changes is not None else tuple(all_changes),
            applied,
            rolled_back,
            using_temporary,
            tuple(rejections),
        )
        if save_report:
            result = replace(
                result, report_path=_save_repair_report(root, result, error=error)
            )
        return result

    try:
        for attempt in range(1, max_attempts + 1):
            files = collect_source_context(work_root, current)
            proposal = client.propose(
                model=model,
                failure=format_failure(current),
                files=files,
                previous_attempt=previous_attempt,
                allow_test_changes=allow_test_changes,
            )
            proposals.append(proposal)
            try:
                changes = validate_changes(
                    work_root,
                    proposal,
                    allow_test_changes=allow_test_changes,
                    allow_destructive_changes=allow_destructive_changes,
                )
            except UnsafePatchError as exc:
                last_rejection = exc
                rejections.append(ProposalRejection(attempt, proposal.summary, str(exc)))
                edit_details = [
                    {
                        "path": edit.path,
                        "start_line": edit.start_line,
                        "incorrect_code": edit.old_text,
                        "corrected_code": edit.new_text,
                    }
                    for edit in proposal.edits
                ]
                previous_attempt = (
                    f"The previous proposal summary was: {proposal.summary}. "
                    f"Its actual edits were: {json.dumps(edit_details, ensure_ascii=False)}. "
                    f"It was rejected before application: {exc}. "
                    "Return a real, minimal production-code change whose corrected_code "
                    "actually implements that summary and differs from incorrect_code."
                )
                continue
            all_changes.extend(changes)

            for change in changes:
                if using_temporary:
                    original = root / change.relative_path
                    original_files.setdefault(change.relative_path, original.read_bytes())
                else:
                    backups.setdefault(change.path, change.path.read_bytes())
            apply_changes(changes)
            current = build_runner(work_root, steps, timeout_seconds=timeout_seconds)
            if current.succeeded:
                final_changes: tuple[ValidatedChange, ...] = tuple(all_changes)
                if using_temporary:
                    consolidated: list[ValidatedChange] = []
                    for relative, original_content in original_files.items():
                        destination = root / relative
                        if apply and destination.read_bytes() != original_content:
                            raise UnsafePatchError(
                                f"Project file changed during isolated repair: {relative}"
                            )
                        replacement = read_utf8_exact(work_root / relative)
                        original_text = read_utf8_exact(destination)
                        if replacement != original_text:
                            reasons = [
                                change.reason
                                for change in all_changes
                                if change.relative_path == relative
                            ]
                            consolidated.append(
                                ValidatedChange(
                                    destination,
                                    relative,
                                    original_text,
                                    replacement,
                                    reasons[-1] if reasons else "verified isolated repair",
                                )
                            )
                    final_changes = tuple(consolidated)
                    if apply:
                        destination_backups = {
                            change.path: change.path.read_bytes() for change in final_changes
                        }
                        try:
                            apply_changes(final_changes)
                        except Exception:
                            restore_files(destination_backups)
                            raise
                result = finish(
                    applied=apply,
                    rolled_back=False,
                    result_changes=final_changes,
                )
                cleanup()
                return result
            previous_attempt = format_failure(current)
    except Exception:
        if backups and not keep_failed and not using_temporary:
            restore_files(backups)
        cleanup()
        raise

    if not backups and last_rejection is not None:
        error = UnsafePatchError(
            f"No safe repair proposal after {max_attempts} attempt(s). Last rejection: {last_rejection}"
        )
        report = finish(applied=False, rolled_back=False, error=str(error))
        setattr(error, "report_path", report.report_path)
        cleanup()
        raise error

    rolled_back = False
    if backups and not keep_failed and not using_temporary:
        restore_files(backups)
        rolled_back = True
    result = finish(
        applied=bool(backups) and not using_temporary,
        rolled_back=rolled_back,
        result_changes=() if using_temporary else None,
    )
    cleanup()
    return result

