"""Preview and differentially validate Python property-test candidates."""

from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autofix.agent.ollama import OllamaClient, PropertyCandidateProposal
from autofix.agent.patches import ValidatedChange, is_test_path
from autofix.agent.plans import RepairPlan, load_repair_plan
from autofix.agent.textio import read_utf8_exact, write_utf8_exact
from autofix.builders import BuildResult, run_build
from autofix.models import BuildStep
from autofix.repository import GitInspectionError, inspect_git_workspace


IGNORED_PARTS = {
    ".autofix",
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
MAX_CANDIDATE_BYTES = 64 * 1024
BANNED_PROPERTY_MODULES = {
    "httpx",
    "os",
    "pathlib",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "urllib",
}
BANNED_PROPERTY_CALLS = {"__import__", "compile", "eval", "exec", "open"}


class PropertyCheckError(ValueError):
    """Raised when a property candidate cannot be safely evaluated."""


@dataclass(frozen=True)
class PropertyCandidate:
    """A reviewable Python/Hypothesis candidate that has not been installed."""

    path: Path
    source: str
    sha256: str
    test_functions: tuple[str, ...]
    target_path: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate identity without duplicating executable source."""

        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": len(self.source.encode("utf-8")),
            "framework": "hypothesis",
            "test_functions": list(self.test_functions),
            "target_path": self.target_path,
        }


@dataclass(frozen=True)
class PropertyRunEvidence:
    """One sanitized isolated command result."""

    phase: str
    attempt: int
    exit_code: int
    succeeded: bool
    timed_out: bool
    duration_seconds: float
    stdout: str
    stderr: str
    failure_signature: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            "exit_code": self.exit_code,
            "succeeded": self.succeeded,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "failure_signature": self.failure_signature,
        }


@dataclass(frozen=True)
class PropertyCheckResult:
    """Preview or differential execution evidence for one property candidate."""

    root: Path
    plan_path: Path
    plan_diff_sha256: str | None
    candidate: PropertyCandidate
    command: BuildStep
    run_requested: bool
    verdict: str
    baseline_runs: tuple[PropertyRunEvidence, ...] = ()
    fixed_runs: tuple[PropertyRunEvidence, ...] = ()
    fixed_verification: tuple[PropertyRunEvidence, ...] = ()
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "counterexample-confirmed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-property-check",
            "version": 1,
            "root": str(self.root),
            "plan_path": str(self.plan_path),
            "plan_diff_sha256": self.plan_diff_sha256,
            "candidate": self.candidate.to_dict(),
            "command": self.command.to_dict(),
            "run_requested": self.run_requested,
            "verdict": self.verdict,
            "passed": self.passed,
            "baseline_runs": [run.to_dict() for run in self.baseline_runs],
            "fixed_runs": [run.to_dict() for run in self.fixed_runs],
            "fixed_verification": [
                run.to_dict() for run in self.fixed_verification
            ],
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class PropertyGenerationResult:
    """A validated, saved, but never executed model-generated candidate."""

    root: Path
    plan_path: Path
    model: str
    rationale: str
    property: str
    candidate: PropertyCandidate
    metadata_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-property-candidate",
            "version": 1,
            "root": str(self.root),
            "plan_path": str(self.plan_path),
            "model": self.model,
            "rationale": self.rationale,
            "property": self.property,
            "candidate": self.candidate.to_dict(),
            "metadata_path": str(self.metadata_path),
            "executed": False,
        }


@dataclass(frozen=True)
class PropertyPromotionResult:
    """Review artifact that proposes adding one confirmed regression test."""

    root: Path
    report_path: Path
    report_sha256: str
    candidate_sha256: str
    plan_path: Path
    target_path: str
    patch_path: Path
    patch_sha256: str
    metadata_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-property-promotion",
            "version": 1,
            "root": str(self.root),
            "report_path": str(self.report_path),
            "report_sha256": self.report_sha256,
            "candidate_sha256": self.candidate_sha256,
            "plan_path": str(self.plan_path),
            "target_path": self.target_path,
            "patch_path": str(self.patch_path),
            "patch_sha256": self.patch_sha256,
            "metadata_path": str(self.metadata_path),
            "applied": False,
        }


@dataclass(frozen=True)
class PropertyApplyResult:
    """Preview or verified application of one promoted regression test."""

    root: Path
    promotion_path: Path
    target_path: str
    candidate_sha256: str
    baseline_commit: str
    apply_requested: bool
    applied: bool
    rolled_back: bool
    verification_steps: tuple[BuildStep, ...]
    verification: BuildResult | None = None
    receipt_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-property-apply",
            "version": 1,
            "root": str(self.root),
            "promotion_path": str(self.promotion_path),
            "target_path": self.target_path,
            "candidate_sha256": self.candidate_sha256,
            "baseline_commit": self.baseline_commit,
            "apply_requested": self.apply_requested,
            "applied": self.applied,
            "rolled_back": self.rolled_back,
            "verification_steps": [
                step.to_dict() for step in self.verification_steps
            ],
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
        }


def validate_property_candidate_source(
    source: str, candidate_path: Path
) -> PropertyCandidate:
    """Validate bounded Hypothesis source before saving or executing it."""

    size = len(source.encode("utf-8"))
    if size == 0 or size > MAX_CANDIDATE_BYTES:
        raise PropertyCheckError(
            f"Property candidate must contain 1-{MAX_CANDIDATE_BYTES} bytes"
        )
    try:
        tree = ast.parse(source, filename=str(candidate_path))
    except SyntaxError as exc:
        raise PropertyCheckError(f"Property candidate has invalid Python syntax: {exc}") from exc

    imports_hypothesis = any(
        (isinstance(node, ast.Import) and any(alias.name == "hypothesis" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "hypothesis")
        for node in ast.walk(tree)
    )
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    forbidden_modules = sorted(imported_modules & BANNED_PROPERTY_MODULES)
    forbidden_calls = sorted(
        {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in BANNED_PROPERTY_CALLS
        }
    )
    if forbidden_modules or forbidden_calls:
        details = ", ".join((*forbidden_modules, *forbidden_calls))
        raise PropertyCheckError(
            f"Property candidate uses forbidden side-effect capability: {details}"
        )
    has_given = any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "given")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "given")
        )
        for node in ast.walk(tree)
    )
    tests = tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )
    if not imports_hypothesis or not has_given or not tests:
        raise PropertyCheckError(
            "Candidate must import Hypothesis, use @given, and define a top-level test_* function"
        )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    target = f".autofix_property_candidates/test_{digest[:12]}.py"
    return PropertyCandidate(candidate_path, source, digest, tests, target)


def inspect_property_candidate(path: Path | str) -> PropertyCandidate:
    """Validate a bounded Hypothesis candidate without executing it."""

    candidate_path = Path(path).expanduser().resolve()
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise PropertyCheckError("Property candidate must be a regular file")
    if candidate_path.suffix.lower() != ".py":
        raise PropertyCheckError("Property candidate must be a Python file")
    try:
        source = read_utf8_exact(candidate_path)
    except UnicodeDecodeError as exc:
        raise PropertyCheckError("Property candidate must be UTF-8") from exc
    return validate_property_candidate_source(source, candidate_path)


def generate_property_candidate(
    root: Path | str,
    plan_path: Path | str,
    client: OllamaClient,
    *,
    model: str,
) -> PropertyGenerationResult:
    """Save one validated model candidate under .autofix without executing it."""

    project_root = Path(root).expanduser().resolve()
    plan, changes = load_repair_plan(plan_path, project_root)
    _validate_plan_policy(plan)
    reviewed_files = {
        change.relative_path: (change.original, change.replacement)
        for change in changes
    }
    proposal: PropertyCandidateProposal = client.generate_property_candidate(
        model=model,
        plan_summary=plan.summary,
        files=reviewed_files,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate_dir = project_root / ".autofix" / "property-candidates"
    prospective = candidate_dir / f"{stamp}.py"
    candidate = validate_property_candidate_source(proposal.content, prospective)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    write_utf8_exact(prospective, proposal.content)
    candidate = replace(candidate, path=prospective)
    metadata_path = candidate_dir / f"{stamp}.json"
    result = PropertyGenerationResult(
        project_root,
        plan.path,
        model,
        proposal.rationale,
        proposal.property,
        candidate,
        metadata_path,
    )
    metadata_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _load_confirmed_report(
    report_path: Path, project_root: Path
) -> tuple[dict[str, Any], PropertyCandidate, Path, str]:
    if not report_path.is_file() or report_path.is_symlink():
        raise PropertyCheckError("Property report must be a regular JSON file")
    report_bytes = report_path.read_bytes()
    try:
        data = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError("Property report must contain valid UTF-8 JSON") from exc
    required = {
        "schema",
        "version",
        "root",
        "plan_path",
        "plan_diff_sha256",
        "candidate",
        "command",
        "run_requested",
        "verdict",
        "passed",
        "baseline_runs",
        "fixed_runs",
        "fixed_verification",
        "report_path",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise PropertyCheckError("Property report has an unsupported structure")
    if (
        data["schema"] != "autofix-property-check"
        or data["version"] != 1
        or data["verdict"] != "counterexample-confirmed"
        or data["passed"] is not True
        or data["run_requested"] is not True
    ):
        raise PropertyCheckError(
            "Only a counterexample-confirmed property report can be promoted"
        )
    baseline_runs = data.get("baseline_runs")
    fixed_runs = data.get("fixed_runs")
    fixed_verification = data.get("fixed_verification")
    evidence_valid = (
        isinstance(baseline_runs, list)
        and len(baseline_runs) == 2
        and all(
            isinstance(item, dict)
            and item.get("phase") == "baseline-candidate"
            and item.get("succeeded") is False
            and isinstance(item.get("failure_signature"), str)
            for item in baseline_runs
        )
        and len({item["failure_signature"] for item in baseline_runs}) == 1
        and isinstance(fixed_runs, list)
        and len(fixed_runs) == 2
        and all(
            isinstance(item, dict)
            and item.get("phase") == "fixed-candidate"
            and item.get("succeeded") is True
            for item in fixed_runs
        )
        and isinstance(fixed_verification, list)
        and bool(fixed_verification)
        and all(
            isinstance(item, dict)
            and item.get("phase") == "fixed-plan-verification"
            and item.get("succeeded") is True
            for item in fixed_verification
        )
    )
    if not evidence_valid:
        raise PropertyCheckError(
            "Property report does not contain complete confirmed differential evidence"
        )
    try:
        reported_root = Path(data["root"]).expanduser().resolve()
        reported_path = Path(data["report_path"]).expanduser().resolve()
        plan_path = Path(data["plan_path"]).expanduser().resolve()
    except TypeError as exc:
        raise PropertyCheckError("Property report contains invalid paths") from exc
    if reported_root != project_root:
        raise PropertyCheckError("Property report belongs to a different project root")
    if reported_path != report_path:
        raise PropertyCheckError("Property report path does not match its saved identity")
    candidate_data = data.get("candidate")
    if not isinstance(candidate_data, dict):
        raise PropertyCheckError("Property report has invalid candidate metadata")
    candidate_path_value = candidate_data.get("path")
    expected_sha256 = candidate_data.get("sha256")
    if not isinstance(candidate_path_value, str) or not isinstance(expected_sha256, str):
        raise PropertyCheckError("Property report has invalid candidate identity")
    candidate = inspect_property_candidate(candidate_path_value)
    if candidate.sha256 != expected_sha256:
        raise PropertyCheckError("Property candidate changed after differential validation")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    return data, candidate, plan_path, report_sha256


def _promotion_target(root: Path, target: str, candidate_sha256: str) -> tuple[str, Path]:
    requested = target.strip() or f"tests/test_autofix_property_{candidate_sha256[:12]}.py"
    relative = Path(requested.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".py":
        raise PropertyCheckError("Promotion target must be a safe relative Python path")
    if not is_test_path(relative):
        raise PropertyCheckError("Promotion target must use a recognized test path")
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise PropertyCheckError("Promotion target escapes the project root") from exc
    if destination.exists():
        raise PropertyCheckError(f"Promotion target already exists: {relative.as_posix()}")
    return relative.as_posix(), destination


def _render_new_test_patch(target_path: str, source: str) -> str:
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    diff_lines = list(
        difflib.unified_diff(
            [],
            normalized.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{target_path}",
        )
    )
    if diff_lines and not diff_lines[-1].endswith("\n"):
        diff_lines[-1] += "\n\\ No newline at end of file\n"
    diff = "".join(diff_lines)
    return (
        f"diff --git a/{target_path} b/{target_path}\n"
        "new file mode 100644\n"
        + diff
    )


def promote_property_candidate(
    root: Path | str,
    report: Path | str,
    *,
    target: str = "",
) -> PropertyPromotionResult:
    """Create an unapplied new-test patch from confirmed differential evidence."""

    project_root = Path(root).expanduser().resolve()
    report_path = Path(report).expanduser().resolve()
    report_data, candidate, plan_path, report_sha256 = _load_confirmed_report(
        report_path, project_root
    )
    plan, _ = load_repair_plan(plan_path, project_root)
    _validate_plan_policy(plan)
    if plan.diff_sha256 != report_data["plan_diff_sha256"]:
        raise PropertyCheckError("Property report is bound to a different plan diff")
    target_path, _ = _promotion_target(
        project_root, target, candidate.sha256
    )
    patch = _render_new_test_patch(target_path, candidate.source)
    patch_bytes = patch.encode("utf-8")
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir = project_root / ".autofix" / "property-patches"
    patch_path = output_dir / f"{stamp}.patch"
    metadata_path = output_dir / f"{stamp}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    patch_created = False
    metadata_created = False
    try:
        with patch_path.open("xb") as stream:
            stream.write(patch_bytes)
        patch_created = True
        result = PropertyPromotionResult(
            project_root,
            report_path,
            report_sha256,
            candidate.sha256,
            plan_path,
            target_path,
            patch_path,
            patch_sha256,
            metadata_path,
        )
        with metadata_path.open("x", encoding="utf-8", newline="") as stream:
            metadata_created = True
            stream.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        for created, artifact in (
            (metadata_created, metadata_path),
            (patch_created, patch_path),
        ):
            if created:
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
        raise PropertyCheckError(f"Unable to write property promotion artifact: {exc}") from exc
    return result


def _load_promotion(
    path: Path, project_root: Path
) -> tuple[dict[str, Any], PropertyCandidate, tuple[BuildStep, ...]]:
    if not path.is_file() or path.is_symlink():
        raise PropertyCheckError("Property promotion metadata must be a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError("Property promotion metadata must be valid UTF-8 JSON") from exc
    required = {
        "schema",
        "version",
        "root",
        "report_path",
        "report_sha256",
        "candidate_sha256",
        "plan_path",
        "target_path",
        "patch_path",
        "patch_sha256",
        "metadata_path",
        "applied",
    }
    if (
        not isinstance(data, dict)
        or set(data) != required
        or data.get("schema") != "autofix-property-promotion"
        or data.get("version") != 1
        or data.get("applied") is not False
    ):
        raise PropertyCheckError("Property promotion metadata has an unsupported structure")
    try:
        recorded_root = Path(data["root"]).expanduser().resolve()
        recorded_metadata = Path(data["metadata_path"]).expanduser().resolve()
        report_path = Path(data["report_path"]).expanduser().resolve()
        plan_path = Path(data["plan_path"]).expanduser().resolve()
        patch_path = Path(data["patch_path"]).expanduser().resolve()
    except TypeError as exc:
        raise PropertyCheckError("Property promotion metadata contains invalid paths") from exc
    if recorded_root != project_root:
        raise PropertyCheckError("Property promotion belongs to a different project root")
    if recorded_metadata != path:
        raise PropertyCheckError("Property promotion path does not match its saved identity")
    report_data, candidate, report_plan, report_sha256 = _load_confirmed_report(
        report_path, project_root
    )
    if report_sha256 != data["report_sha256"]:
        raise PropertyCheckError("Confirmed property report changed after promotion")
    if candidate.sha256 != data["candidate_sha256"]:
        raise PropertyCheckError("Promoted candidate identity no longer matches")
    if report_plan != plan_path:
        raise PropertyCheckError("Promotion and report reference different repair plans")
    if not patch_path.is_file() or patch_path.is_symlink():
        raise PropertyCheckError("Promoted property patch is missing or unsafe")
    patch_bytes = patch_path.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != data["patch_sha256"]:
        raise PropertyCheckError("Promoted property patch changed after review")
    target_path, _ = _promotion_target(
        project_root, data["target_path"], candidate.sha256
    )
    expected_patch = _render_new_test_patch(target_path, candidate.source).encode("utf-8")
    if patch_bytes != expected_patch:
        raise PropertyCheckError("Promoted patch does not match the confirmed candidate")
    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError("Repair plan is unavailable or malformed") from exc
    raw_changes = plan_data.get("changes") if isinstance(plan_data, dict) else None
    verification = plan_data.get("verification") if isinstance(plan_data, dict) else None
    if not isinstance(raw_changes, list) or not raw_changes:
        raise PropertyCheckError("Repair plan has no reviewed changes")
    for change in raw_changes:
        if (
            not isinstance(change, dict)
            or not isinstance(change.get("path"), str)
            or not isinstance(change.get("content"), str)
        ):
            raise PropertyCheckError("Repair plan contains invalid reviewed changes")
        relative = Path(change["path"])
        current = (project_root / relative).resolve()
        try:
            current.relative_to(project_root)
        except ValueError as exc:
            raise PropertyCheckError("Repair plan path escapes the project root") from exc
        if not current.is_file() or read_utf8_exact(current) != change["content"]:
            raise PropertyCheckError(
                f"Reviewed repair is not applied exactly: {relative.as_posix()}"
            )
    if not isinstance(verification, dict) or verification.get("runner") != "local":
        raise PropertyCheckError("Property apply requires a local plan verification policy")
    raw_steps = verification.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PropertyCheckError("Repair plan has no verification steps")
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
            raise PropertyCheckError("Repair plan contains invalid verification steps")
        steps.append(BuildStep(raw["label"], tuple(raw["argv"])))
    if report_data["plan_diff_sha256"] != plan_data.get("diff_sha256"):
        raise PropertyCheckError("Property report no longer matches the repair plan diff")
    property_step = BuildStep(
        "Run promoted property regression",
        (sys.executable, "-m", "pytest", target_path, "-q"),
    )
    return data, candidate, (property_step, *steps)


def apply_property_promotion(
    root: Path | str,
    promotion: Path | str,
    *,
    apply: bool = False,
    timeout_seconds: float = 300,
    save_receipt: bool = True,
    receipt_output: Path | str | None = None,
    build_runner: Callable[..., BuildResult] = run_build,
) -> PropertyApplyResult:
    """Preview or explicitly add and verify one promoted regression test."""

    project_root = Path(root).expanduser().resolve()
    promotion_path = Path(promotion).expanduser().resolve()
    try:
        git_status = inspect_git_workspace(project_root)
    except GitInspectionError as exc:
        raise PropertyCheckError(f"Unable to inspect Git workspace: {exc}") from exc
    if (
        not git_status.is_repository
        or git_status.head_commit is None
        or not git_status.clean
    ):
        raise PropertyCheckError(
            "Property apply requires a clean Git repository with a committed repair"
        )
    data, candidate, steps = _load_promotion(promotion_path, project_root)
    result = PropertyApplyResult(
        project_root,
        promotion_path,
        data["target_path"],
        candidate.sha256,
        git_status.head_commit,
        apply,
        False,
        False,
        steps,
    )
    if not apply:
        return result

    destination = project_root / data["target_path"]
    created_directories: list[Path] = []
    parent = destination.parent
    while parent != project_root and not parent.exists():
        created_directories.append(parent)
        parent = parent.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("x", encoding="utf-8", newline="") as stream:
            stream.write(candidate.source)
        created = True
        verification = build_runner(
            project_root,
            steps,
            timeout_seconds=timeout_seconds,
            save_report=False,
        )
        applied = verification.succeeded
        rolled_back = not applied
        if rolled_back:
            destination.unlink()
            created = False
            for directory in created_directories:
                try:
                    directory.rmdir()
                except OSError:
                    break
        result = replace(
            result,
            applied=applied,
            rolled_back=rolled_back,
            verification=verification,
        )
        if save_receipt or receipt_output is not None:
            if receipt_output is None:
                receipt_dir = project_root / ".autofix" / "property-applies"
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                receipt_path = receipt_dir / f"{stamp}.json"
            else:
                receipt_path = Path(receipt_output).expanduser().resolve()
            result = replace(result, receipt_path=receipt_path)
            _save_property_apply_receipt(receipt_path, result)
        return result
    except Exception:
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in created_directories:
            try:
                directory.rmdir()
            except OSError:
                break
        raise


def _save_property_apply_receipt(
    destination: Path, result: PropertyApplyResult
) -> None:
    """Publish a complete receipt without overwriting an existing artifact."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PropertyCheckError(f"Property apply receipt already exists: {destination}")
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n"
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
            stream.write(payload)
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
        raise PropertyCheckError(
            f"Unable to publish property apply receipt: {exc}"
        ) from exc


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    ignored = {name for name in names if name in IGNORED_PARTS}
    ignored.update(name for name in names if (base / name).is_symlink())
    return ignored


def _install_candidate(root: Path, candidate: PropertyCandidate) -> None:
    target = root / candidate.target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    write_utf8_exact(target, candidate.source)


def _apply_fixed_changes(root: Path, changes: tuple[ValidatedChange, ...]) -> None:
    for change in changes:
        target = root / change.relative_path
        if not target.is_file():
            raise PropertyCheckError(
                f"Isolated fixed workspace is missing planned path: {change.relative_path}"
            )
        write_utf8_exact(target, change.replacement)


def _sanitize_output(value: str, root: Path) -> str:
    sanitized = value.replace(str(root), "<isolated-project>")
    sanitized = sanitized.replace(str(root).replace("\\", "/"), "<isolated-project>")
    return sanitized


def _failure_signature(evidence: PropertyRunEvidence) -> str | None:
    if evidence.succeeded:
        return None
    output = f"{evidence.exit_code}\n{evidence.stdout}\n{evidence.stderr}"
    output = re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <duration>", output)
    output = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", output)
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def _run_evidence(
    root: Path,
    step: BuildStep,
    *,
    phase: str,
    attempt: int,
    timeout_seconds: float,
    build_runner: Callable[..., BuildResult],
) -> PropertyRunEvidence:
    result = build_runner(
        root,
        (step,),
        timeout_seconds=timeout_seconds,
        save_report=False,
    )
    if not result.steps:
        raise PropertyCheckError("Property command produced no execution evidence")
    executed = result.steps[-1]
    evidence = PropertyRunEvidence(
        phase,
        attempt,
        executed.exit_code,
        executed.succeeded,
        executed.timed_out,
        executed.duration_seconds,
        _sanitize_output(executed.stdout, root),
        _sanitize_output(executed.stderr, root),
        None,
    )
    return replace(evidence, failure_signature=_failure_signature(evidence))


def _save_result(result: PropertyCheckResult) -> PropertyCheckResult:
    report_dir = result.root / ".autofix" / "property-checks"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_path = report_dir / f"{stamp}.json"
    saved = replace(result, report_path=report_path)
    report_path.write_text(
        json.dumps(saved.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return saved


def _validate_plan_policy(plan: RepairPlan) -> tuple[BuildStep, ...]:
    if not plan.verification_steps:
        raise PropertyCheckError(
            "Property validation requires a plan with bound verification steps"
        )
    if plan.verification_runner != "local":
        raise PropertyCheckError(
            "Property validation currently requires a locally verified repair plan"
        )
    return plan.verification_steps


def check_property_candidate(
    root: Path | str,
    candidate_path: Path | str,
    plan_path: Path | str,
    *,
    run: bool = False,
    timeout_seconds: float = 300,
    save_report: bool = True,
    build_runner: Callable[..., BuildResult] = run_build,
) -> PropertyCheckResult:
    """Preview or differentially validate a candidate in two isolated copies."""

    project_root = Path(root).expanduser().resolve()
    candidate = inspect_property_candidate(candidate_path)
    plan, changes = load_repair_plan(plan_path, project_root)
    verification_steps = _validate_plan_policy(plan)
    command = BuildStep(
        "Run isolated property candidate",
        (sys.executable, "-m", "pytest", candidate.target_path, "-q"),
    )
    result = PropertyCheckResult(
        project_root,
        plan.path,
        plan.diff_sha256,
        candidate,
        command,
        run,
        "preview" if not run else "pending",
    )
    if not run:
        return result
    missing = [
        package
        for package in ("pytest", "hypothesis")
        if importlib.util.find_spec(package) is None
    ]
    if missing:
        raise PropertyCheckError(
            "Property execution requires already-installed packages: "
            + ", ".join(missing)
        )

    with tempfile.TemporaryDirectory(prefix="autofix-property-") as temporary:
        temporary_root = Path(temporary)
        baseline_root = temporary_root / "baseline"
        fixed_root = temporary_root / "fixed"
        shutil.copytree(project_root, baseline_root, ignore=_copy_ignore)
        shutil.copytree(project_root, fixed_root, ignore=_copy_ignore)
        _install_candidate(baseline_root, candidate)
        _install_candidate(fixed_root, candidate)
        _apply_fixed_changes(fixed_root, changes)

        baseline_runs = tuple(
            _run_evidence(
                baseline_root,
                command,
                phase="baseline-candidate",
                attempt=attempt,
                timeout_seconds=timeout_seconds,
                build_runner=build_runner,
            )
            for attempt in (1, 2)
        )
        if any(item.succeeded for item in baseline_runs):
            result = replace(
                result,
                verdict="baseline-not-failing",
                baseline_runs=baseline_runs,
            )
        elif len({item.failure_signature for item in baseline_runs}) != 1:
            result = replace(
                result,
                verdict="baseline-unstable",
                baseline_runs=baseline_runs,
            )
        else:
            fixed_runs = tuple(
                _run_evidence(
                    fixed_root,
                    command,
                    phase="fixed-candidate",
                    attempt=attempt,
                    timeout_seconds=timeout_seconds,
                    build_runner=build_runner,
                )
                for attempt in (1, 2)
            )
            if not all(item.succeeded for item in fixed_runs):
                result = replace(
                    result,
                    verdict="fixed-candidate-failed",
                    baseline_runs=baseline_runs,
                    fixed_runs=fixed_runs,
                )
            else:
                verification: list[PropertyRunEvidence] = []
                for index, step in enumerate(verification_steps, 1):
                    evidence = _run_evidence(
                        fixed_root,
                        step,
                        phase="fixed-plan-verification",
                        attempt=index,
                        timeout_seconds=timeout_seconds,
                        build_runner=build_runner,
                    )
                    verification.append(evidence)
                    if not evidence.succeeded:
                        break
                verdict = (
                    "counterexample-confirmed"
                    if verification and all(item.succeeded for item in verification)
                    else "fixed-verification-failed"
                )
                result = replace(
                    result,
                    verdict=verdict,
                    baseline_runs=baseline_runs,
                    fixed_runs=fixed_runs,
                    fixed_verification=tuple(verification),
                )
    return _save_result(result) if save_report else result
