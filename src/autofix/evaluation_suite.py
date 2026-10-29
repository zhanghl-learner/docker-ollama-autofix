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
from typing import Any

from autofix.evaluation import (
    EvaluationError,
    EvaluationResult,
    evaluate_project,
    load_evaluation_manifest,
)


SUITE_SCHEMA = "autofix-evaluation-suite"
SUITE_VERSION = 1
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize one case without dropping its failure evidence."""

        manifest = self.result.manifest if self.result else None
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
            "error": self.error,
            "evaluation": self.result.to_dict() if self.result else None,
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class EvaluationSuiteResult:
    """Aggregate result for every discovered baseline contract."""

    root: Path
    cases: tuple[EvaluationSuiteCase, ...]
    started_at: str
    duration_seconds: float
    report_dir: Path | None = None
    json_report_path: Path | None = None
    markdown_report_path: Path | None = None

    @property
    def passed(self) -> bool:
        """Require at least one case and no omitted or failed case."""

        return bool(self.cases) and all(case.passed for case in self.cases)

    @property
    def metrics(self) -> dict[str, Any]:
        """Return stable baseline counts and grouping summaries."""

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
        return {
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize the versioned aggregate and every original case result."""

        return {
            "schema": SUITE_SCHEMA,
            "version": SUITE_VERSION,
            "mode": "baseline",
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
        f"- Mode: `baseline`",
        f"- Root: `{result.root}`",
        f"- Passed: **{metrics['passed']} / {metrics['total']}**",
        f"- Pass rate: **{metrics['pass_rate']:.2%}**",
        f"- Duration: **{result.duration_seconds:.3f}s**",
        "",
        "| Case | Language | Difficulty | Status | Duration | Error |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for case in result.cases:
        manifest = case.result.manifest if case.result else None
        error = (case.error or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{case.relative_root}`",
                    manifest.language if manifest else "unknown",
                    manifest.difficulty if manifest else "unknown",
                    case.status,
                    f"{case.duration_seconds:.3f}s",
                    error,
                )
            )
            + " |"
        )
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
) -> EvaluationSuiteResult:
    """Run every discovered baseline contract and retain all observed outcomes."""

    suite_root = Path(root).expanduser().resolve()
    case_roots = discover_evaluation_cases(suite_root)
    if not case_roots:
        raise EvaluationSuiteError(
            f"No autofix-eval.yml files found under {suite_root}"
        )
    started_at = datetime.now(timezone.utc).isoformat()
    began = monotonic()
    cases: list[EvaluationSuiteCase] = []
    for case_root in case_roots:
        relative = case_root.relative_to(suite_root).as_posix() or "."
        manifest_path = case_root / "autofix-eval.yml"
        case_began = monotonic()
        try:
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError as exc:
            cases.append(
                EvaluationSuiteCase(
                    case_root,
                    relative,
                    "",
                    round(monotonic() - case_began, 3),
                    error_kind="invalid_manifest",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        try:
            manifest = load_evaluation_manifest(case_root)
        except (EvaluationError, OSError, UnicodeError) as exc:
            cases.append(
                EvaluationSuiteCase(
                    case_root,
                    relative,
                    digest,
                    round(monotonic() - case_began, 3),
                    error_kind="invalid_manifest",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        try:
            evaluation = evaluate_project(
                case_root,
                manifest,
                timeout_seconds=timeout_seconds,
                save_report=False,
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
        suite_root,
        tuple(cases),
        started_at,
        round(monotonic() - began, 3),
    )
    if save_report:
        try:
            result = _save_suite(suite_root, result)
        except OSError as exc:
            raise EvaluationSuiteError(f"Unable to save evaluation suite: {exc}") from exc
    return result
