"""Orchestrate build diagnosis, model retries, isolation, and verified commit."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from autofix.agent.artifacts import save_standalone_patch
from autofix.agent.context import collect_source_context, format_failure
from autofix.agent.diagnostics import RepairEnvironmentError, environment_failure
from autofix.agent.ollama import RepairProposal
from autofix.agent.patches import (
    UnsafePatchError,
    ValidatedChange,
    apply_changes,
    restore_files,
    validate_changes,
    validated_diff_sha256,
)
from autofix.agent.textio import read_utf8_exact
from autofix.builders import BuildResult, run_build
from autofix.models import BuildStep
from autofix.repository import (
    DetachedGitWorktree,
    GitInspectionError,
    GitWorktreeError,
    create_detached_worktree,
    validate_git_repair_workspace,
)


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
    """Protocol implemented by any backend that can propose bounded edits."""

    def propose(
        self,
        *,
        model: str,
        failure: str,
        files: dict[str, str],
        previous_attempt: str | None = None,
        allow_test_changes: bool = False,
    ) -> RepairProposal:
        """Return one bounded proposal for the supplied failure context."""

        ...


@dataclass(frozen=True)
class RepairResult:
    """Complete repair outcome used by the CLI and persistent audit report."""

    initial_build: BuildResult
    final_build: BuildResult
    proposals: tuple[RepairProposal, ...]
    changes: tuple[ValidatedChange, ...]
    applied: bool
    rolled_back: bool
    isolated: bool = False
    rejections: tuple["ProposalRejection", ...] = ()
    verification_failures: tuple["VerificationFailure", ...] = ()
    report_path: Path | None = None
    patch_path: Path | None = None
    patch_sha256: str | None = None
    project_root: Path | None = None
    workspace_kind: str = "original"
    repository_root: Path | None = None
    baseline_commit: str | None = None
    repository_fingerprint: str | None = None
    project_subpath: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return the final build verdict."""

        return self.final_build.succeeded

    @property
    def diff_sha256(self) -> str | None:
        """Return the canonical verified diff identity when changes exist."""

        return validated_diff_sha256(self.changes) if self.changes else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize audit data without duplicating full proposal contents."""

        return {
            "version": 2,
            "type": "repair",
            "root": str(self.project_root or self.initial_build.root),
            "succeeded": self.succeeded,
            "applied": self.applied,
            "rolled_back": self.rolled_back,
            "isolated": self.isolated,
            "workspace": {
                "kind": self.workspace_kind,
                "repository_root": (
                    str(self.repository_root) if self.repository_root else None
                ),
                "baseline_commit": self.baseline_commit,
                "repository_fingerprint": self.repository_fingerprint,
                "project_subpath": self.project_subpath,
            },
            "diff_sha256": self.diff_sha256,
            "patch_path": str(self.patch_path) if self.patch_path else None,
            "patch_sha256": self.patch_sha256,
            "initial_build": self.initial_build.to_dict(),
            "final_build": self.final_build.to_dict(),
            "proposals": [
                {
                    "summary": proposal.summary,
                    "diagnosis": proposal.diagnosis,
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
            "verification_failures": [
                failure.to_dict() for failure in self.verification_failures
            ],
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
    """Static validation failure returned to the model as retry feedback."""

    attempt: int
    summary: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize a rejected attempt for the repair report."""

        return {"attempt": self.attempt, "summary": self.summary, "reason": self.reason}


@dataclass(frozen=True)
class VerificationFailure:
    """A statically valid proposal whose resulting build still failed."""

    attempt: int
    summary: str
    diagnosis: str
    changes: tuple[ValidatedChange, ...]
    build: BuildResult

    def to_dict(self) -> dict[str, Any]:
        """Serialize the attempted diff and its deterministic build result."""

        return {
            "attempt": self.attempt,
            "summary": self.summary,
            "diagnosis": self.diagnosis,
            "changes": [
                {
                    "path": change.relative_path,
                    "reason": change.reason,
                    "diff": change.diff,
                }
                for change in self.changes
            ],
            "build": self.build.to_dict(),
        }


def _proposal_fingerprint(proposal: RepairProposal) -> str:
    """Return a stable identity for detecting an exactly repeated proposal."""

    payload = {
        "changes": [
            {"path": item.path, "content": item.content, "reason": item.reason}
            for item in proposal.changes
        ],
        "edits": [
            {
                "path": item.path,
                "start_line": item.start_line,
                "delete_lines": item.delete_lines,
                "old_text": item.old_text,
                "new_text": item.new_text,
                "reason": item.reason,
            }
            for item in proposal.edits
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verification_retry_feedback(
    failures: list[VerificationFailure],
    *,
    baseline: BuildResult,
    latest_rejection: str | None = None,
) -> str:
    """Give the next attempt cumulative diffs and their resulting diagnostics."""

    def failed_checks(build: BuildResult) -> set[str]:
        """Extract common unittest and pytest failure identifiers for deltas."""

        output = "\n".join(
            f"{step.stdout}\n{step.stderr}" for step in build.steps
        )
        checks = set(
            re.findall(r"^(test\S+) .* \.\.\. (?:FAIL|ERROR)$", output, re.MULTILINE)
        )
        checks.update(re.findall(r"^FAILED\s+(\S+)", output, re.MULTILINE))
        return checks

    baseline_checks = failed_checks(baseline)
    sections: list[str] = []
    for item in failures[-3:]:
        diff = "\n".join(change.diff for change in item.changes)
        failure = format_failure(item.build)
        changed_locations = ", ".join(
            f"{change.relative_path} "
            + " ".join(re.findall(r"@@[^@]+@@", change.diff))
            for change in item.changes
        )
        new_failures = sorted(failed_checks(item.build) - baseline_checks)
        regression = (
            "\nNEWLY FAILING CHECKS CAUSED BY THIS DIFF: " + ", ".join(new_failures)
            if new_failures
            else ""
        )
        sections.append(
            f"ATTEMPT {item.attempt}: {item.summary}\n"
            f"MODEL DIAGNOSIS: {item.diagnosis or 'not provided'}\n"
            f"FAILED EDIT LOCATIONS: {changed_locations}\n"
            "APPLIED DIFF:\n"
            f"{diff}\n"
            "VERIFICATION FAILURE AFTER THAT DIFF:\n"
            f"{failure}{regression}"
        )
    history = "\n\n".join(sections)
    # Current numbered files are sent separately and remain the source of truth
    # for the next coordinates, so bound historical diagnostics aggressively.
    if len(history) > 24_000:
        history = "... [older retry history truncated]\n" + history[-24_000:]
    rejection = (
        f"\nLATEST PROPOSAL REJECTION:\n{latest_rejection}\n"
        if latest_rejection
        else ""
    )
    return (
        "Earlier proposals passed static validation but did not pass verification.\n"
        f"{history}{rejection}\n"
        "Do not repeat, reverse, or cosmetically rewrite an earlier failed change. "
        "An edit limited to a previously failed location is invalid unless it also "
        "changes a different causal condition. If a diff introduced a newly failing "
        "check, treat the edited statement as previously correct for that behavior. "
        "Compare all observed outcomes with the current numbered source. Trace "
        "whether each failing boundary input reaches the edited statement, then "
        "repair the causal condition, control flow, or operation with a different "
        "minimal edit."
    )


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
    patch_output: Path | str | None = None,
    build_runner: Callable[..., BuildResult] = run_build,
) -> RepairResult:
    """Build, request bounded repairs, validate, rebuild, and optionally write back.

    Preview and isolated apply operations mutate only a temporary workspace.
    Original files are written only after the complete build matrix succeeds and
    a byte-level concurrency check confirms they have not changed meanwhile.
    """

    root = root.resolve()
    try:
        git_status = validate_git_repair_workspace(root)
    except (GitInspectionError, GitWorktreeError) as exc:
        raise UnsafePatchError(str(exc)) from exc

    temporary: tempfile.TemporaryDirectory[str] | None = None
    git_worktree: DetachedGitWorktree | None = None
    work_root = root
    using_temporary = isolate or not apply
    workspace_kind = "original"
    if using_temporary:
        if git_status.is_repository:
            try:
                git_worktree = create_detached_worktree(git_status)
            except GitWorktreeError as exc:
                raise UnsafePatchError(str(exc)) from exc
            work_root = git_worktree.project_root
            workspace_kind = "git-worktree"
        else:
            temporary = tempfile.TemporaryDirectory(prefix="autofix-repair-")
            work_root = Path(temporary.name) / "workspace"
            try:
                shutil.copytree(root, work_root, ignore=_workspace_ignore)
            except Exception:
                temporary.cleanup()
                raise
            workspace_kind = "temporary-copy"

    def cleanup() -> None:
        """Release copy/worktree files and Git administrative metadata."""

        if git_worktree is not None:
            try:
                git_worktree.close()
            except GitWorktreeError as exc:
                raise UnsafePatchError(str(exc)) from exc
        if temporary is not None:
            temporary.cleanup()

    def cleanup_after_error(error: BaseException) -> None:
        """Preserve the primary failure while recording any cleanup failure."""

        try:
            cleanup()
        except Exception as cleanup_error:
            error.add_note(f"Temporary workspace cleanup also failed: {cleanup_error}")

    def run_workspace_build() -> BuildResult:
        """Execute inside isolation while keeping reports bound to the real project."""

        result = build_runner(work_root, steps, timeout_seconds=timeout_seconds)
        if work_root != root:
            return replace(result, root=root, report_path=None)
        return result

    try:
        initial = run_workspace_build()
    except BaseException as exc:
        cleanup_after_error(exc)
        raise
    if initial.succeeded:
        result = RepairResult(
            initial,
            initial,
            (),
            (),
            False,
            False,
            using_temporary,
            project_root=root,
            workspace_kind=workspace_kind,
            repository_root=git_status.repository_root,
            baseline_commit=git_status.head_commit,
            repository_fingerprint=git_status.repository_fingerprint,
            project_subpath=git_status.project_subpath,
        )
        if save_report:
            result = replace(result, report_path=_save_repair_report(root, result))
        cleanup()
        return result
    environment_issue = environment_failure(initial)
    if environment_issue:
        result = RepairResult(
            initial,
            initial,
            (),
            (),
            False,
            False,
            using_temporary,
            project_root=root,
            workspace_kind=workspace_kind,
            repository_root=git_status.repository_root,
            baseline_commit=git_status.head_commit,
            repository_fingerprint=git_status.repository_fingerprint,
            project_subpath=git_status.project_subpath,
        )
        if save_report:
            result = replace(
                result,
                report_path=_save_repair_report(root, result, error=environment_issue),
            )
        error = RepairEnvironmentError(environment_issue)
        setattr(error, "report_path", result.report_path)
        cleanup_after_error(error)
        raise error

    current = initial
    proposals: list[RepairProposal] = []
    all_changes: list[ValidatedChange] = []
    backups: dict[Path, bytes] = {}
    previous_attempt: str | None = None
    last_rejection: UnsafePatchError | None = None
    rejections: list[ProposalRejection] = []
    verification_failures: list[VerificationFailure] = []
    proposal_fingerprints: set[str] = set()
    seen_file_versions: dict[str, set[str]] = {}
    original_files: dict[str, bytes] = {}

    def finish(
        *,
        applied: bool,
        rolled_back: bool,
        error: str | None = None,
        result_changes: tuple[ValidatedChange, ...] | None = None,
    ) -> RepairResult:
        """Assemble and optionally persist the current repair outcome."""

        selected_changes = (
            result_changes if result_changes is not None else tuple(all_changes)
        )
        patch_path: Path | None = None
        patch_sha256: str | None = None
        if current.succeeded and selected_changes and (save_report or patch_output is not None):
            artifact = save_standalone_patch(
                root, selected_changes, destination=patch_output
            )
            patch_path = artifact.path
            patch_sha256 = artifact.sha256
        result = RepairResult(
            initial,
            current,
            tuple(proposals),
            selected_changes,
            applied,
            rolled_back,
            using_temporary,
            tuple(rejections),
            tuple(verification_failures),
            project_root=root,
            workspace_kind=workspace_kind,
            repository_root=git_status.repository_root,
            baseline_commit=git_status.head_commit,
            repository_fingerprint=git_status.repository_fingerprint,
            project_subpath=git_status.project_subpath,
            patch_path=patch_path,
            patch_sha256=patch_sha256,
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
            fingerprint = _proposal_fingerprint(proposal)
            if fingerprint in proposal_fingerprints:
                last_rejection = UnsafePatchError(
                    "Duplicate proposal repeats an earlier edit without progress"
                )
                rejections.append(
                    ProposalRejection(attempt, proposal.summary, str(last_rejection))
                )
                previous_attempt = _verification_retry_feedback(
                    verification_failures,
                    baseline=initial,
                    latest_rejection=str(last_rejection),
                )
                continue
            proposal_fingerprints.add(fingerprint)
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
                latest_feedback = (
                    f"The previous proposal summary was: {proposal.summary}. "
                    f"Its actual edits were: {json.dumps(edit_details, ensure_ascii=False)}. "
                    f"It was rejected before application: {exc}. "
                    "Return a real, minimal production-code change whose corrected_code "
                    "actually implements that summary and differs from incorrect_code."
                )
                previous_attempt = (
                    _verification_retry_feedback(
                        verification_failures,
                        baseline=initial,
                        latest_rejection=latest_feedback,
                    )
                    if verification_failures
                    else latest_feedback
                )
                continue
            repeated_state = True
            for change in changes:
                versions = seen_file_versions.setdefault(
                    change.relative_path,
                    {hashlib.sha256(change.original.encode("utf-8")).hexdigest()},
                )
                replacement_hash = hashlib.sha256(
                    change.replacement.encode("utf-8")
                ).hexdigest()
                if replacement_hash not in versions:
                    repeated_state = False
            if changes and repeated_state:
                last_rejection = UnsafePatchError(
                    "Repair cycle detected: proposal restores a previously observed "
                    "failed source state"
                )
                rejections.append(
                    ProposalRejection(attempt, proposal.summary, str(last_rejection))
                )
                previous_attempt = _verification_retry_feedback(
                    verification_failures,
                    baseline=initial,
                    latest_rejection=str(last_rejection),
                )
                continue
            for change in changes:
                if using_temporary:
                    original = root / change.relative_path
                    original_files.setdefault(change.relative_path, original.read_bytes())
                else:
                    backups.setdefault(change.path, change.path.read_bytes())
            attempt_backups = {
                change.path: change.path.read_bytes() for change in changes
            }
            apply_changes(changes)
            for change in changes:
                seen_file_versions[change.relative_path].add(
                    hashlib.sha256(change.replacement.encode("utf-8")).hexdigest()
                )
            current = run_workspace_build()
            if current.succeeded:
                all_changes.extend(changes)
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
            verification_failures.append(
                VerificationFailure(
                    attempt, proposal.summary, proposal.diagnosis, changes, current
                )
            )
            previous_attempt = _verification_retry_feedback(
                verification_failures, baseline=initial
            )
            if not keep_failed:
                # Each retry starts from the same known failing baseline. This
                # prevents a later model call from having to undo an earlier bad
                # edit before it can address the original cause.
                restore_files(attempt_backups)
                current = initial
            else:
                all_changes.extend(changes)
    except BaseException as exc:
        if backups and not keep_failed and not using_temporary:
            restore_files(backups)
        cleanup_after_error(exc)
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

