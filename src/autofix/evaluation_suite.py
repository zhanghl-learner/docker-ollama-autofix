"""Discover evaluation fixtures and aggregate reproducible baseline evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from autofix import __version__
from autofix.agent.repair import RepairClient, RepairProgressEvent, RepairResult
from autofix.builders import BuildResult, run_build
from autofix.evaluation import (
    EvaluationError,
    EvaluationResult,
    evaluate_project,
    load_evaluation_manifest,
)


SUITE_SCHEMA = "autofix-evaluation-suite"
BASELINE_SUITE_VERSION = 1
REPAIR_SUITE_VERSION = 3
_IGNORED_DIRECTORIES = {
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


class EvaluationSuiteError(ValueError):
    """Raised when a suite root or aggregate output is unusable."""


@dataclass(frozen=True)
class EvaluationSuiteProgressEvent:
    """One case-level or repair-level milestone in a sequential suite run."""

    phase: str
    case_root: Path
    relative_root: str
    case_index: int
    case_total: int
    duration_seconds: float = 0.0
    status: str | None = None
    repair: RepairProgressEvent | None = None


EvaluationSuiteProgressCallback = Callable[[EvaluationSuiteProgressEvent], None]


@dataclass(frozen=True)
class EvaluationSuiteCase:
    """One discovered case, including malformed and failed contracts."""

    case_root: Path
    relative_root: str
    contract_sha256: str
    duration_seconds: float
    result: EvaluationResult | None = None
    error_kind: str | None = None
    error: str | None = None
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        """Return whether this case produced a passing baseline result."""

        return self.result is not None and self.result.passed and self.error is None

    @property
    def status(self) -> str:
        """Return a stable per-case status for JSON and Markdown reports."""

        if self.error_kind is not None:
            return self.error_kind
        return "passed" if self.passed else "failed"

    @property
    def failure_summary(self) -> str | None:
        """Explain a failed case without requiring consumers to inspect raw builds."""

        if self.passed:
            return None
        if self.error:
            return self.error
        if self.result is None:
            return "Evaluation did not produce a result"
        if self.result.failure_contract_mismatches:
            return "; ".join(self.result.failure_contract_mismatches)
        if self.result.repair_error:
            return self.result.repair_error
        evidence = _repair_evidence(self.result)
        if evidence and evidence.verification_failures:
            count = len(evidence.verification_failures)
            last_build = evidence.verification_failures[-1].build
            failed_step = last_build.steps[-1] if last_build.steps else None
            suffix = (
                f"; last failure: {failed_step.label} exited {failed_step.exit_code}"
                if failed_step
                else ""
            )
            return (
                f"{count} proposal(s) passed static checks but failed verification"
                f"{suffix}"
            )
        if evidence and evidence.rejections:
            return (
                f"{len(evidence.rejections)} proposal(s) rejected by safety checks; "
                f"last rejection: {evidence.rejections[-1].reason}"
            )
        if self.result.unexpected_change_paths:
            return "Repair changed paths outside the evaluation contract"
        if self.result.missing_required_paths:
            return "Repair did not change every required path"
        return "Evaluation contract failed"

    def to_dict(self) -> dict[str, Any]:
        """Serialize one case without dropping its failure evidence."""

        manifest = self.result.manifest if self.result else None
        evaluation = self.result.to_dict() if self.result else None
        if evaluation is not None and self.result and self.result.mode == "repair":
            evidence = _repair_evidence(self.result)
            evaluation["repair_details"] = evidence.to_dict() if evidence else None
        return {
            "root": str(self.case_root),
            "relative_root": self.relative_root,
            "name": manifest.name if manifest else None,
            "language": manifest.language if manifest else None,
            "difficulty": manifest.difficulty if manifest else None,
            "tags": list(manifest.tags) if manifest else [],
            "contract_sha256": self.contract_sha256,
            "status": self.status,
            "passed": self.passed,
            "duration_seconds": self.duration_seconds,
            "error_kind": self.error_kind,
            "error": self.error or self.failure_summary,
            "evaluation": evaluation,
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class EvaluationSuiteResult:
    """Aggregate result for every discovered baseline or repair contract."""

    root: Path
    cases: tuple[EvaluationSuiteCase, ...]
    started_at: str
    duration_seconds: float
    mode: str = "baseline"
    model: str | None = None
    runner: str = "local"
    max_attempts: int | None = None
    autofix_version: str = __version__
    report_dir: Path | None = None
    json_report_path: Path | None = None
    markdown_report_path: Path | None = None

    @property
    def passed(self) -> bool:
        """Require at least one case and no omitted or failed case."""

        return bool(self.cases) and all(case.passed for case in self.cases)

    @property
    def metrics(self) -> dict[str, Any]:
        """Return stable common counts plus repair metrics when applicable."""

        languages: dict[str, dict[str, int]] = {}
        difficulties: dict[str, dict[str, int]] = {}
        for case in self.cases:
            language = case.result.manifest.language if case.result else "unknown"
            difficulty = case.result.manifest.difficulty if case.result else "unknown"
            for groups, key in ((languages, language), (difficulties, difficulty)):
                bucket = groups.setdefault(key, {"total": 0, "passed": 0, "failed": 0})
                bucket["total"] += 1
                bucket["passed" if case.passed else "failed"] += 1
        passed = sum(case.passed for case in self.cases)
        total = len(self.cases)
        metrics: dict[str, Any] = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "errors": sum(case.error_kind is not None for case in self.cases),
            "invalid_manifests": sum(
                case.error_kind == "invalid_manifest" for case in self.cases
            ),
            "execution_errors": sum(
                case.error_kind == "execution_error" for case in self.cases
            ),
            "duplicate_case_names": sum(
                case.error_kind == "duplicate_case_name" for case in self.cases
            ),
            "pass_rate": round(passed / total, 6) if total else 0.0,
            "by_language": languages,
            "by_difficulty": difficulties,
        }
        if self.mode == "repair":
            repair_results = [
                case.result
                for case in self.cases
                if case.result is not None and case.result.mode == "repair"
            ]
            proposal_counts = [
                _evaluation_count(result, "proposals") for result in repair_results
            ]
            attempted = sum(count > 0 for count in proposal_counts)
            eligible = sum(
                result.failure_contract_matched for result in repair_results
            )
            proposals = sum(proposal_counts)
            rejections = sum(
                _evaluation_count(result, "rejections") for result in repair_results
            )
            verification_failures = sum(
                _evaluation_count(result, "verification_failures")
                for result in repair_results
            )
            first_attempt_passed = sum(
                case.passed
                and case.result is not None
                and _evaluation_count(case.result, "proposals") == 1
                and _evaluation_count(case.result, "rejections") == 0
                and _evaluation_count(case.result, "verification_failures") == 0
                for case in self.cases
            )
            unexpected = sum(
                bool(result.unexpected_change_paths) for result in repair_results
            )
            missing_required = sum(
                bool(result.missing_required_paths) for result in repair_results
            )
            metrics.update(
                {
                    "repair_eligible": eligible,
                    "repair_attempted": attempted,
                    "model_not_attempted": len(repair_results) - attempted,
                    "first_attempt_passed": first_attempt_passed,
                    "first_attempt_pass_rate": _rate(first_attempt_passed, attempted),
                    "total_proposals": proposals,
                    "average_attempts": round(proposals / attempted, 6)
                    if attempted
                    else 0.0,
                    "static_rejections": rejections,
                    "static_rejection_rate": _rate(rejections, proposals),
                    "verification_failures": verification_failures,
                    "verification_failure_rate": _rate(
                        verification_failures, proposals
                    ),
                    "unexpected_change_cases": unexpected,
                    "unexpected_change_rate": _rate(unexpected, attempted),
                    "missing_required_change_cases": missing_required,
                    "missing_required_change_rate": _rate(
                        missing_required, attempted
                    ),
                    **_context_quality_metrics(repair_results),
                }
            )
        return metrics

    def to_dict(self) -> dict[str, Any]:
        """Serialize the versioned aggregate and every original case result."""

        payload = {
            "schema": SUITE_SCHEMA,
            "version": (
                REPAIR_SUITE_VERSION if self.mode == "repair" else BASELINE_SUITE_VERSION
            ),
            "mode": self.mode,
            "root": str(self.root),
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "passed": self.passed,
            "metrics": self.metrics,
            "cases": [case.to_dict() for case in self.cases],
            "report_dir": str(self.report_dir) if self.report_dir else None,
            "json_report_path": (
                str(self.json_report_path) if self.json_report_path else None
            ),
            "markdown_report_path": (
                str(self.markdown_report_path) if self.markdown_report_path else None
            ),
        }
        if self.mode == "repair":
            payload.update(
                {
                    "model": self.model,
                    "runner": self.runner,
                    "max_attempts": self.max_attempts,
                    "autofix_version": self.autofix_version,
                }
            )
        return payload


def _evaluation_count(result: EvaluationResult, kind: str) -> int:
    """Read attempt counts uniformly from successful and rejected repairs."""

    if kind == "proposals":
        return len(result.repair.proposals) if result.repair else result.proposal_count
    if kind == "rejections":
        return len(result.repair.rejections) if result.repair else result.rejection_count
    if kind == "verification_failures":
        return (
            len(result.repair.verification_failures)
            if result.repair
            else result.verification_failure_count
        )
    raise ValueError(f"Unknown evaluation count: {kind}")


def _rate(numerator: int, denominator: int) -> float:
    """Return a stable zero-safe ratio for machine-readable metrics."""

    return round(numerator / denominator, 6) if denominator else 0.0


def _repair_evidence(result: EvaluationResult) -> RepairResult | None:
    """Return successful, failed-verification, or rejected repair evidence."""

    return result.repair or result.repair_evidence


def _context_quality_metrics(results: list[EvaluationResult]) -> dict[str, Any]:
    """Measure bounded context cost and required-path retrieval quality."""

    context_cases = 0
    context_attempts = 0
    files_sent = 0
    chars_sent = 0
    truncated_files = 0
    fallback_files = 0
    required_paths = 0
    required_hits = 0
    required_top5_hits = 0
    required_ranks: list[int] = []
    for result in results:
        evidence = _repair_evidence(result)
        if evidence is None or not evidence.context_selections:
            continue
        non_empty_attempts = [items for items in evidence.context_selections if items]
        if not non_empty_attempts:
            continue
        context_cases += 1
        context_attempts += len(non_empty_attempts)
        for selections in non_empty_attempts:
            files_sent += len(selections)
            chars_sent += sum(item.chars for item in selections)
            truncated_files += sum(item.truncated for item in selections)
            fallback_files += sum(
                item.reason == "project-source-fallback" for item in selections
            )

        first_attempt = non_empty_attempts[0]
        ranks = {item.path: rank for rank, item in enumerate(first_attempt, 1)}
        for required in result.manifest.required_change_paths:
            required_paths += 1
            rank = ranks.get(required)
            if rank is None:
                continue
            required_hits += 1
            required_ranks.append(rank)
            if rank <= 5:
                required_top5_hits += 1

    return {
        "context_cases": context_cases,
        "context_attempts": context_attempts,
        "context_files_sent": files_sent,
        "context_chars_sent": chars_sent,
        "context_truncated_files": truncated_files,
        "context_truncation_rate": _rate(truncated_files, files_sent),
        "context_fallback_files": fallback_files,
        "context_fallback_rate": _rate(fallback_files, files_sent),
        "required_context_paths": required_paths,
        "required_context_hits": required_hits,
        "required_context_recall": _rate(required_hits, required_paths),
        "required_context_top5_hits": required_top5_hits,
        "required_context_top5_recall": _rate(
            required_top5_hits, required_paths
        ),
        "average_required_context_rank": (
            round(sum(required_ranks) / len(required_ranks), 6)
            if required_ranks
            else 0.0
        ),
    }


def _markdown_code(
    text: str, *, language: str = "text", limit: int = 4000
) -> list[str]:
    """Render a bounded code block while preserving full output in JSON."""

    normalized = text.replace("```", "` ` `")
    if len(normalized) > limit:
        normalized = normalized[:limit] + "\n... output truncated in Markdown; see JSON ..."
    return [f"```{language}", normalized.rstrip(), "```"]


def discover_evaluation_cases(root: Path | str) -> tuple[Path, ...]:
    """Find manifests deterministically without traversing generated directories."""

    suite_root = Path(root).expanduser().resolve()
    if not suite_root.is_dir():
        raise EvaluationSuiteError(f"Evaluation suite root is not a directory: {suite_root}")
    manifests: list[Path] = []
    for directory, names, files in os.walk(suite_root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _IGNORED_DIRECTORIES
            and not (Path(directory) / name).is_symlink()
        )
        if "autofix-eval.yml" in files:
            manifests.append(Path(directory).resolve())
    return tuple(sorted(manifests, key=lambda path: path.as_posix()))


def _case_id(relative_root: str) -> str:
    """Create a filesystem-safe stable identifier from a relative case root."""

    safe = "root" if relative_root == "." else "__".join(Path(relative_root).parts)
    normalized = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in safe
    )
    suffix = hashlib.sha256(relative_root.encode("utf-8")).hexdigest()[:10]
    return f"{normalized}-{suffix}"


def _markdown(result: EvaluationSuiteResult) -> str:
    """Render a compact human-readable report without hiding failed cases."""

    metrics = result.metrics
    lines = [
        "# AutoFix evaluation suite",
        "",
        f"- Mode: `{result.mode}`",
        f"- Root: `{result.root}`",
        f"- Passed: **{metrics['passed']} / {metrics['total']}**",
        f"- Pass rate: **{metrics['pass_rate']:.2%}**",
        f"- Duration: **{result.duration_seconds:.3f}s**",
    ]
    if result.mode == "repair":
        lines.extend(
            [
                f"- Model: `{result.model}`",
                f"- Runner: `{result.runner}`",
                f"- Maximum attempts per case: **{result.max_attempts}**",
                f"- First-attempt pass rate: **{metrics['first_attempt_pass_rate']:.2%}**",
                f"- Average attempts: **{metrics['average_attempts']:.3f}**",
                f"- Static rejection rate: **{metrics['static_rejection_rate']:.2%}**",
                f"- Verification failure rate: **{metrics['verification_failure_rate']:.2%}**",
                f"- Required-path context recall: **{metrics['required_context_recall']:.2%}**",
                f"- Required-path Top-5 recall: **{metrics['required_context_top5_recall']:.2%}**",
                f"- Average required-path rank: **{metrics['average_required_context_rank']:.3f}**",
                f"- Context sent: **{metrics['context_files_sent']} files / {metrics['context_chars_sent']} characters**",
            ]
        )
    lines.extend(
        [
            "",
            "| Case | Language | Difficulty | Status | Attempts | Duration | Error |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for case in result.cases:
        manifest = case.result.manifest if case.result else None
        error = (case.failure_summary or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{case.relative_root}`",
                    manifest.language if manifest else "unknown",
                    manifest.difficulty if manifest else "unknown",
                    case.status,
                    str(_evaluation_count(case.result, "proposals"))
                    if case.result and result.mode == "repair"
                    else "-",
                    f"{case.duration_seconds:.3f}s",
                    error,
                )
            )
            + " |"
        )
    failed_repairs = [
        case
        for case in result.cases
        if not case.passed
        and case.result is not None
        and _repair_evidence(case.result) is not None
    ]
    if failed_repairs:
        lines.extend(["", "## Failed repair evidence"])
    for case in failed_repairs:
        case_result = case.result
        assert case_result is not None
        evidence = _repair_evidence(case_result)
        assert evidence is not None
        lines.extend(["", f"### `{case.relative_root}`"])
        for rejection in evidence.rejections:
            lines.extend(
                [
                    "",
                    f"#### Attempt {rejection.attempt}: static rejection",
                    "",
                    f"- Summary: {rejection.summary}",
                    f"- Reason: {rejection.reason}",
                ]
            )
        for failure in evidence.verification_failures:
            lines.extend(
                [
                    "",
                    f"#### Attempt {failure.attempt}: verification failure",
                    "",
                    f"- Summary: {failure.summary}",
                    f"- Diagnosis: {failure.diagnosis}",
                ]
            )
            for change in failure.changes:
                lines.extend(["", f"Changed `{change.relative_path}`:", ""])
                lines.extend(_markdown_code(change.diff, language="diff"))
            if failure.build.steps:
                step = failure.build.steps[-1]
                lines.extend(
                    [
                        "",
                        f"Failed step: `{step.label}` (exit `{step.exit_code}`)",
                        "",
                    ]
                )
                output = "\n".join(
                    part for part in (step.stdout, step.stderr) if part
                )
                lines.extend(_markdown_code(output or "(no captured output)"))
    return "\n".join(lines) + "\n"


def _save_suite(root: Path, result: EvaluationSuiteResult) -> EvaluationSuiteResult:
    """Atomically publish aggregate JSON, Markdown, and per-case JSON files."""

    parent = root / ".autofix" / "evaluation-suites"
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / stamp
    staging = Path(tempfile.mkdtemp(prefix=f".{stamp}.", dir=parent))
    try:
        cases_dir = staging / "cases"
        cases_dir.mkdir()
        staged_cases: list[EvaluationSuiteCase] = []
        for case in result.cases:
            report_path = cases_dir / f"{_case_id(case.relative_root)}.json"
            staged_case = replace(case, report_path=destination / "cases" / report_path.name)
            report_path.write_text(
                json.dumps(staged_case.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            staged_cases.append(staged_case)
        published = replace(
            result,
            cases=tuple(staged_cases),
            report_dir=destination,
            json_report_path=destination / "suite.json",
            markdown_report_path=destination / "suite.md",
        )
        (staging / "suite.json").write_text(
            json.dumps(published.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "suite.md").write_text(_markdown(published), encoding="utf-8")
        os.replace(staging, destination)
        return published
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def evaluate_suite(
    root: Path | str,
    *,
    timeout_seconds: float = 300,
    save_report: bool = True,
    client: RepairClient | None = None,
    model: str | None = None,
    max_attempts: int = 3,
    build_runner: Callable[..., BuildResult] = run_build,
    runner_name: str = "local",
    progress_callback: EvaluationSuiteProgressCallback | None = None,
) -> EvaluationSuiteResult:
    """Run every contract in baseline or isolated repair mode and retain outcomes."""

    suite_root = Path(root).expanduser().resolve()
    mode = "repair" if client is not None else "baseline"
    selected_model = model.strip() if isinstance(model, str) else None
    if not 1 <= max_attempts <= 20:
        raise EvaluationSuiteError("max_attempts must be between 1 and 20")
    if timeout_seconds <= 0:
        raise EvaluationSuiteError("timeout_seconds must be greater than zero")
    if runner_name not in {"local", "docker"}:
        raise EvaluationSuiteError("runner_name must be 'local' or 'docker'")
    if mode == "repair" and not selected_model:
        raise EvaluationSuiteError("A model is required for repair evaluation")
    case_roots = discover_evaluation_cases(suite_root)
    if not case_roots:
        raise EvaluationSuiteError(
            f"No autofix-eval.yml files found under {suite_root}"
        )
    started_at = datetime.now(timezone.utc).isoformat()
    began = monotonic()
    cases: list[EvaluationSuiteCase] = []
    case_total = len(case_roots)
    for case_index, case_root in enumerate(case_roots, start=1):
        relative = case_root.relative_to(suite_root).as_posix() or "."
        manifest_path = case_root / "autofix-eval.yml"
        case_began = monotonic()
        if progress_callback is not None:
            progress_callback(
                EvaluationSuiteProgressEvent(
                    "case_started",
                    case_root,
                    relative,
                    case_index,
                    case_total,
                )
            )

        def emit_case_completed(case: EvaluationSuiteCase) -> None:
            if progress_callback is not None:
                progress_callback(
                    EvaluationSuiteProgressEvent(
                        "case_completed",
                        case_root,
                        relative,
                        case_index,
                        case_total,
                        case.duration_seconds,
                        case.status,
                    )
                )

        def forward_repair(event: RepairProgressEvent) -> None:
            if progress_callback is not None:
                progress_callback(
                    EvaluationSuiteProgressEvent(
                        "repair_progress",
                        case_root,
                        relative,
                        case_index,
                        case_total,
                        round(monotonic() - case_began, 3),
                        repair=event,
                    )
                )

        try:
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError as exc:
            case = EvaluationSuiteCase(
                case_root,
                relative,
                "",
                round(monotonic() - case_began, 3),
                error_kind="invalid_manifest",
                error=f"{type(exc).__name__}: {exc}",
            )
            cases.append(case)
            emit_case_completed(case)
            continue
        try:
            manifest = load_evaluation_manifest(case_root)
        except (EvaluationError, OSError, UnicodeError) as exc:
            case = EvaluationSuiteCase(
                case_root,
                relative,
                digest,
                round(monotonic() - case_began, 3),
                error_kind="invalid_manifest",
                error=f"{type(exc).__name__}: {exc}",
            )
            cases.append(case)
            emit_case_completed(case)
            continue
        try:
            evaluation = evaluate_project(
                case_root,
                manifest,
                client=client,
                model=selected_model,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                build_runner=build_runner,
                runner_name=runner_name,
                save_report=False,
                progress_callback=forward_repair if client is not None else None,
            )
            case = EvaluationSuiteCase(
                case_root,
                relative,
                digest,
                round(monotonic() - case_began, 3),
                result=evaluation,
            )
        except Exception as exc:
            case = EvaluationSuiteCase(
                case_root,
                relative,
                digest,
                round(monotonic() - case_began, 3),
                error_kind="execution_error",
                error=f"{type(exc).__name__}: {exc}",
            )
        cases.append(case)
        emit_case_completed(case)
    names: dict[str, list[int]] = {}
    for index, case in enumerate(cases):
        if case.result is not None:
            names.setdefault(case.result.manifest.name, []).append(index)
    for name, indexes in names.items():
        if len(indexes) > 1:
            locations = ", ".join(cases[index].relative_root for index in indexes)
            for index in indexes:
                cases[index] = replace(
                    cases[index],
                    error_kind="duplicate_case_name",
                    error=f"Duplicate case name {name!r}: {locations}",
                )
    result = EvaluationSuiteResult(
        root=suite_root,
        cases=tuple(cases),
        started_at=started_at,
        duration_seconds=round(monotonic() - began, 3),
        mode=mode,
        model=selected_model,
        runner=runner_name,
        max_attempts=max_attempts if mode == "repair" else None,
    )
    if save_report:
        try:
            result = _save_suite(suite_root, result)
        except OSError as exc:
            raise EvaluationSuiteError(f"Unable to save evaluation suite: {exc}") from exc
    return result
