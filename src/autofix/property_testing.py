"""Preview and differentially validate Python property-test candidates."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autofix.agent.patches import ValidatedChange
from autofix.agent.plans import RepairPlan, load_repair_plan
from autofix.agent.textio import read_utf8_exact, write_utf8_exact
from autofix.builders import BuildResult, run_build
from autofix.models import BuildStep


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


def inspect_property_candidate(path: Path | str) -> PropertyCandidate:
    """Validate a bounded Hypothesis candidate without executing it."""

    candidate_path = Path(path).expanduser().resolve()
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise PropertyCheckError("Property candidate must be a regular file")
    if candidate_path.suffix.lower() != ".py":
        raise PropertyCheckError("Property candidate must be a Python file")
    size = candidate_path.stat().st_size
    if size == 0 or size > MAX_CANDIDATE_BYTES:
        raise PropertyCheckError(
            f"Property candidate must contain 1-{MAX_CANDIDATE_BYTES} bytes"
        )
    try:
        source = read_utf8_exact(candidate_path)
        tree = ast.parse(source, filename=str(candidate_path))
    except UnicodeDecodeError as exc:
        raise PropertyCheckError("Property candidate must be UTF-8") from exc
    except SyntaxError as exc:
        raise PropertyCheckError(f"Property candidate has invalid Python syntax: {exc}") from exc

    imports_hypothesis = any(
        (isinstance(node, ast.Import) and any(alias.name == "hypothesis" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "hypothesis")
        for node in ast.walk(tree)
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
