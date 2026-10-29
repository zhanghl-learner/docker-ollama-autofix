"""Run reproducible, scope-aware AutoFix benchmark cases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml

from autofix.agent.diagnostics import RepairEnvironmentError
from autofix.agent.ollama import OllamaError
from autofix.agent.patches import UnsafePatchError
from autofix.agent.repair import RepairClient, RepairResult, repair_project
from autofix.builders import BuildResult, parse_command, run_build
from autofix.models import BuildStep


class EvaluationError(ValueError):
    """Raised when an evaluation contract is missing or unsafe."""


@dataclass(frozen=True)
class FailureExpectation:
    """Expected location and output signature of an intentional failure."""

    exit_code: int | None = None
    failed_step: int | None = None
    output_contains: tuple[str, ...] = ()
    output_excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationManifest:
    """Validated contract describing one intentionally failing project."""

    path: Path
    version: int
    name: str
    language: str
    difficulty: str
    commands: tuple[str, ...]
    allowed_change_paths: tuple[str, ...]
    required_change_paths: tuple[str, ...]
    tags: tuple[str, ...]
    expected_failure: FailureExpectation | None

    @property
    def steps(self) -> tuple[BuildStep, ...]:
        """Convert declared commands into an ordered verification matrix."""

        steps: list[BuildStep] = []
        for index, command in enumerate(self.commands, start=1):
            parsed = parse_command(command)
            steps.append(BuildStep(f"Evaluation step {index}", parsed.argv))
        return tuple(steps)


@dataclass(frozen=True)
class EvaluationResult:
    """Baseline or model-assisted benchmark outcome with scope metrics."""

    root: Path
    manifest: EvaluationManifest
    mode: str
    build: BuildResult
    repair: RepairResult | None
    changed_paths: tuple[str, ...]
    unexpected_change_paths: tuple[str, ...]
    missing_required_paths: tuple[str, ...]
    proposal_count: int = 0
    rejection_count: int = 0
    verification_failure_count: int = 0
    model: str | None = None
    runner: str = "local"
    repair_error: str | None = None
    repair_report_path: Path | None = None
    report_path: Path | None = None
    # In-memory evidence lets aggregate callers retain rejected attempts even
    # when report persistence is disabled. It is intentionally serialized only
    # by the suite layer, which already owns its versioned output contract.
    repair_evidence: RepairResult | None = None

    @property
    def initial_failure_confirmed(self) -> bool:
        """Return whether the fixture really failed before any proposed repair."""

        initial = self.repair.initial_build if self.repair else self.build
        return not initial.succeeded

    @property
    def failure_contract_mismatches(self) -> tuple[str, ...]:
        """Explain why the observed initial failure differs from the contract."""

        initial = self.repair.initial_build if self.repair else self.build
        expectation = self.manifest.expected_failure
        mismatches: list[str] = []
        if initial.succeeded:
            return ("build unexpectedly succeeded",)
        if expectation is None:
            return ()
        if expectation.exit_code is not None and initial.exit_code != expectation.exit_code:
            mismatches.append(
                f"exit code {initial.exit_code} != expected {expectation.exit_code}"
            )
        failed_step = len(initial.steps)
        if expectation.failed_step is not None and failed_step != expectation.failed_step:
            mismatches.append(
                f"failed step {failed_step} != expected {expectation.failed_step}"
            )
        output = "\n".join(
            part
            for step in initial.steps
            for part in (step.stdout, step.stderr)
            if part
        )
        for text in expectation.output_contains:
            if text not in output:
                mismatches.append(f"required output is missing: {text!r}")
        for text in expectation.output_excludes:
            if text in output:
                mismatches.append(f"forbidden output is present: {text!r}")
        return tuple(mismatches)

    @property
    def failure_contract_matched(self) -> bool:
        """Return whether the initial failure matches its declared signature."""

        return self.initial_failure_confirmed and not self.failure_contract_mismatches

    @property
    def passed(self) -> bool:
        """Apply the manifest contract to the observed evaluation outcome."""

        if not self.failure_contract_matched:
            return False
        if self.mode == "baseline":
            return True
        return bool(
            self.repair
            and self.repair.succeeded
            and not self.unexpected_change_paths
            and not self.missing_required_paths
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize benchmark metadata and stable comparison metrics."""

        initial = self.repair.initial_build if self.repair else self.build
        return {
            "version": 1,
            "type": "evaluation",
            "root": str(self.root),
            "manifest": str(self.manifest.path),
            "case": {
                "name": self.manifest.name,
                "language": self.manifest.language,
                "difficulty": self.manifest.difficulty,
                "tags": list(self.manifest.tags),
                "expected_failure": {
                    "exit_code": self.manifest.expected_failure.exit_code,
                    "failed_step": self.manifest.expected_failure.failed_step,
                    "output_contains": list(
                        self.manifest.expected_failure.output_contains
                    ),
                    "output_excludes": list(
                        self.manifest.expected_failure.output_excludes
                    ),
                }
                if self.manifest.expected_failure
                else None,
            },
            "mode": self.mode,
            "model": self.model,
            "runner": self.runner,
            "passed": self.passed,
            "metrics": {
                "initial_failure_confirmed": self.initial_failure_confirmed,
                "failure_contract_matched": self.failure_contract_matched,
                "failure_contract_mismatches": list(
                    self.failure_contract_mismatches
                ),
                "repair_succeeded": self.repair.succeeded
                if self.repair
                else (False if self.mode == "repair" else None),
                "proposal_count": len(self.repair.proposals)
                if self.repair
                else self.proposal_count,
                "rejection_count": len(self.repair.rejections)
                if self.repair
                else self.rejection_count,
                "verification_failure_count": len(
                    self.repair.verification_failures
                )
                if self.repair
                else self.verification_failure_count,
                "changed_paths": list(self.changed_paths),
                "unexpected_change_paths": list(self.unexpected_change_paths),
                "missing_required_paths": list(self.missing_required_paths),
                "verification_step_count": len(self.manifest.commands),
            },
            "initial_build": initial.to_dict(),
            "final_build": self.repair.final_build.to_dict() if self.repair else None,
            "repair_error": self.repair_error,
            "repair_report": str(self.repair.report_path)
            if self.repair and self.repair.report_path
            else str(self.repair_report_path)
            if self.repair_report_path
            else None,
            "report_path": str(self.report_path) if self.report_path else None,
        }


_ALLOWED_KEYS = {
    "version",
    "name",
    "language",
    "difficulty",
    "commands",
    "allowed_change_paths",
    "required_change_paths",
    "tags",
    "expected_failure",
}

_EXPECTED_FAILURE_KEYS = {
    "exit_code",
    "failed_step",
    "output_contains",
    "output_excludes",
}


def _string_list(raw: dict[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EvaluationError(f"{key} must be a list of non-empty strings")
    result = tuple(item.strip() for item in value)
    if required and not result:
        raise EvaluationError(f"{key} must contain at least one item")
    if len(set(result)) != len(result):
        raise EvaluationError(f"{key} must not contain duplicates")
    return result


def _safe_relative_path(root: Path, value: str, key: str) -> str:
    """Normalize a contract path and guarantee it remains inside the case root."""

    portable = value.replace("\\", "/")
    relative = PurePosixPath(portable)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise EvaluationError(f"Unsafe path in {key}: {value}")
    normalized = relative.as_posix()
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvaluationError(f"Path escapes the evaluation root in {key}: {value}") from exc
    if not candidate.is_file():
        raise EvaluationError(f"Path in {key} is not an existing file: {value}")
    return normalized


def _load_failure_expectation(
    raw: dict[str, Any], command_count: int
) -> FailureExpectation | None:
    """Validate an optional expected-failure signature from a manifest."""

    value = raw.get("expected_failure")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EvaluationError("expected_failure must be a mapping")
    unknown = sorted(set(value) - _EXPECTED_FAILURE_KEYS)
    if unknown:
        raise EvaluationError(
            f"Unknown expected_failure field(s): {', '.join(unknown)}"
        )

    exit_code = value.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0
    ):
        raise EvaluationError("expected_failure.exit_code must be a non-zero integer")
    failed_step = value.get("failed_step")
    if failed_step is not None and (
        not isinstance(failed_step, int)
        or isinstance(failed_step, bool)
        or not 1 <= failed_step <= command_count
    ):
        raise EvaluationError(
            "expected_failure.failed_step must identify a declared command"
        )

    contains = _string_list(value, "output_contains")
    excludes = _string_list(value, "output_excludes")
    overlap = sorted(set(contains) & set(excludes))
    if overlap:
        raise EvaluationError(
            "expected_failure output cannot be both required and forbidden: "
            + ", ".join(overlap)
        )
    if exit_code is None and failed_step is None and not contains and not excludes:
        raise EvaluationError("expected_failure must contain at least one assertion")
    return FailureExpectation(exit_code, failed_step, contains, excludes)


def load_evaluation_manifest(
    root: Path | str, filename: str = "autofix-eval.yml"
) -> EvaluationManifest:
    """Read and strictly validate an evaluation contract from a case directory."""

    project_root = Path(root).expanduser().resolve()
    path = project_root / filename
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise EvaluationError(f"Unable to read evaluation manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvaluationError("Evaluation manifest must be a mapping")
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise EvaluationError(f"Unknown evaluation field(s): {', '.join(unknown)}")
    if raw.get("version") != 1:
        raise EvaluationError("Only evaluation manifest version 1 is supported")

    def required_string(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise EvaluationError(f"{key} must be a non-empty string")
        return value.strip()

    commands = _string_list(raw, "commands", required=True)
    for command in commands:
        try:
            parse_command(command)
        except ValueError as exc:
            raise EvaluationError(f"Invalid evaluation command: {exc}") from exc
    allowed = tuple(
        _safe_relative_path(project_root, value, "allowed_change_paths")
        for value in _string_list(raw, "allowed_change_paths", required=True)
    )
    required = tuple(
        _safe_relative_path(project_root, value, "required_change_paths")
        for value in _string_list(raw, "required_change_paths")
    )
    outside = sorted(set(required) - set(allowed))
    if outside:
        raise EvaluationError(
            "required_change_paths must be a subset of allowed_change_paths: "
            + ", ".join(outside)
        )
    return EvaluationManifest(
        path=path,
        version=1,
        name=required_string("name"),
        language=required_string("language"),
        difficulty=required_string("difficulty"),
        commands=commands,
        allowed_change_paths=allowed,
        required_change_paths=required,
        tags=_string_list(raw, "tags"),
        expected_failure=_load_failure_expectation(raw, len(commands)),
    )


def _save_evaluation_report(root: Path, result: EvaluationResult) -> Path:
    report_dir = root / ".autofix" / "evaluations"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = report_dir / f"{stamp}.json"
    data = result.to_dict()
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["contract_sha256"] = hashlib.sha256(
        result.manifest.path.read_bytes()
    ).hexdigest()
    data["report_path"] = str(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def evaluate_project(
    root: Path | str,
    manifest: EvaluationManifest,
    *,
    client: RepairClient | None = None,
    model: str | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 300,
    build_runner: Callable[..., BuildResult] = run_build,
    runner_name: str = "local",
    save_report: bool = True,
) -> EvaluationResult:
    """Validate a failing baseline and optionally score a non-mutating repair."""

    project_root = Path(root).expanduser().resolve()
    if manifest.path.parent.resolve() != project_root:
        raise EvaluationError("Evaluation manifest does not belong to the project root")
    baseline = build_runner(
        project_root,
        manifest.steps,
        timeout_seconds=timeout_seconds,
        save_report=False,
    )
    if client is None:
        result = EvaluationResult(
            project_root,
            manifest,
            "baseline",
            baseline,
            None,
            (),
            (),
            (),
            runner=runner_name,
        )
    else:
        if not model or not model.strip():
            raise EvaluationError("A model is required for repair evaluation")
        preflight = EvaluationResult(
            project_root,
            manifest,
            "repair",
            baseline,
            None,
            (),
            (),
            manifest.required_change_paths,
            model=model.strip(),
            runner=runner_name,
        )
        if not preflight.failure_contract_matched:
            result = replace(
                preflight,
                repair_error=(
                    "Initial failure does not match expected_failure; model repair "
                    "was not attempted"
                ),
            )
            if save_report:
                result = replace(
                    result, report_path=_save_evaluation_report(project_root, result)
                )
            return result
        try:
            repair = repair_project(
                project_root,
                manifest.steps,
                client,
                model=model.strip(),
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                apply=False,
                isolate=True,
                save_report=save_report,
                build_runner=build_runner,
            )
        except (UnsafePatchError, RepairEnvironmentError, OllamaError) as exc:
            # A rejected or unavailable model is an observed benchmark result,
            # not a malformed benchmark, so retain the preflight baseline.
            repair_report_path = getattr(exc, "report_path", None)
            proposal_count = 0
            rejection_count = 0
            verification_failure_count = 0
            repair_result = getattr(exc, "repair_result", None)
            if isinstance(repair_result, RepairResult):
                proposal_count = len(repair_result.proposals)
                rejection_count = len(repair_result.rejections)
                verification_failure_count = len(
                    repair_result.verification_failures
                )
            if repair_report_path:
                try:
                    repair_data = json.loads(
                        Path(repair_report_path).read_text(encoding="utf-8")
                    )
                    proposals = repair_data.get("proposals", [])
                    rejections = repair_data.get("rejections", [])
                    verification_failures = repair_data.get(
                        "verification_failures", []
                    )
                    proposal_count = len(proposals) if isinstance(proposals, list) else 0
                    rejection_count = (
                        len(rejections) if isinstance(rejections, list) else 0
                    )
                    verification_failure_count = (
                        len(verification_failures)
                        if isinstance(verification_failures, list)
                        else 0
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
            result = EvaluationResult(
                project_root,
                manifest,
                "repair",
                baseline,
                None,
                (),
                (),
                manifest.required_change_paths,
                proposal_count=proposal_count,
                rejection_count=rejection_count,
                verification_failure_count=verification_failure_count,
                model=model.strip(),
                runner=runner_name,
                repair_error=str(exc),
                repair_report_path=repair_report_path,
                repair_evidence=repair_result
                if isinstance(repair_result, RepairResult)
                else None,
            )
            if save_report:
                result = replace(
                    result, report_path=_save_evaluation_report(project_root, result)
                )
            return result
        changed = tuple(sorted({change.relative_path for change in repair.changes}))
        allowed = set(manifest.allowed_change_paths)
        required = set(manifest.required_change_paths)
        result = EvaluationResult(
            project_root,
            manifest,
            "repair",
            repair.initial_build,
            repair,
            changed,
            tuple(sorted(set(changed) - allowed)),
            tuple(sorted(required - set(changed))),
            proposal_count=len(repair.proposals),
            rejection_count=len(repair.rejections),
            verification_failure_count=len(repair.verification_failures),
            model=model.strip(),
            runner=runner_name,
        )
    if save_report:
        result = replace(result, report_path=_save_evaluation_report(project_root, result))
    return result
