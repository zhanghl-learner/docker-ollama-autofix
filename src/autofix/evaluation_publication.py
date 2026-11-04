"""Review and publish privacy-minimized, versioned model evaluation evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.benchmark import (
    BenchmarkError,
    evaluation_case_identity,
    load_evaluation_suite,
    verify_frozen_benchmark,
)
from autofix.evaluation_suite import (
    discover_evaluation_cases,
    expected_toolchain_commands,
)
from autofix.evaluation import EvaluationError, load_evaluation_manifest
from autofix.property_apply import PropertyApplyPolicyError, publish_json_exclusive


PUBLICATION_SCHEMA = "autofix-evaluation-publication"
PUBLICATION_VERSION = 2
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class EvaluationPublicationError(ValueError):
    """Raised when suite evidence is unsafe or incomplete for publication."""


@dataclass(frozen=True)
class EvaluationPublication:
    """Canonical public attestation derived from reviewed private suite evidence."""

    release: str
    reviewed_at: str
    reviewed_by: str
    review_note: str
    accepted_failed_suite: bool
    benchmark: dict[str, Any]
    source_suite_sha256: str
    source_suite_version: int
    suite_started_at: str
    suite_passed: bool
    autofix_version: str
    model: str
    model_identity: dict[str, Any]
    runner: str
    runtime_identity: dict[str, Any]
    max_attempts: int
    case_set_sha256: str
    metrics: dict[str, Any]
    cases: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the path-free, source-bound publication envelope."""

        return {
            "schema": PUBLICATION_SCHEMA,
            "version": PUBLICATION_VERSION,
            "release": self.release,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "accepted_failed_suite": self.accepted_failed_suite,
            "benchmark": self.benchmark,
            "source_suite_sha256": self.source_suite_sha256,
            "source_suite_version": self.source_suite_version,
            "suite_started_at": self.suite_started_at,
            "suite_passed": self.suite_passed,
            "autofix_version": self.autofix_version,
            "model": self.model,
            "model_identity": self.model_identity,
            "runner": self.runner,
            "runtime_identity": self.runtime_identity,
            "max_attempts": self.max_attempts,
            "case_set_sha256": self.case_set_sha256,
            "metrics": self.metrics,
            "cases": list(self.cases),
        }


def _required_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationPublicationError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise EvaluationPublicationError(f"{label} exceeds {maximum} characters")
    return normalized


def _validate_release(release: str) -> str:
    normalized = _required_text(release, "Release", 64)
    if not _SAFE_COMPONENT.fullmatch(normalized):
        raise EvaluationPublicationError(
            "Release must use only letters, numbers, dot, underscore, or hyphen"
        )
    return normalized


def _current_contracts(
    benchmark_root: Path,
) -> tuple[dict[str, str], set[str], dict[str, dict[str, str]]]:
    contracts: dict[str, str] = {}
    languages: set[str] = set()
    metadata: dict[str, dict[str, str]] = {}
    for case_root in discover_evaluation_cases(benchmark_root):
        relative = case_root.relative_to(benchmark_root).as_posix()
        manifest_path = case_root / "autofix-eval.yml"
        contracts[relative] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        try:
            manifest = load_evaluation_manifest(case_root)
        except (OSError, UnicodeError, EvaluationError) as exc:
            raise EvaluationPublicationError(
                f"Unable to read evaluation language: {relative}"
            ) from exc
        languages.add(manifest.language)
        metadata[relative] = {
            "name": manifest.name,
            "language": manifest.language,
            "difficulty": manifest.difficulty,
        }
    return contracts, languages, metadata


def _validate_metrics(data: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise EvaluationPublicationError("Suite metrics are missing")

    def validate_metric_value(value: Any) -> None:
        if isinstance(value, dict):
            if not all(isinstance(key, str) and key for key in value):
                raise EvaluationPublicationError("Suite metrics contain an invalid key")
            for nested in value.values():
                validate_metric_value(nested)
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise EvaluationPublicationError(
                "Suite metrics must contain finite non-negative numbers only"
            )

    validate_metric_value(metrics)
    passed = sum(case.get("passed") is True for case in cases)
    expected = {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
    }
    for key, value in expected.items():
        if metrics.get(key) != value:
            raise EvaluationPublicationError(f"Suite metric is inconsistent: {key}")
    pass_rate = metrics.get("pass_rate")
    expected_rate = round(passed / len(cases), 6)
    if (
        isinstance(pass_rate, bool)
        or not isinstance(pass_rate, (int, float))
        or not math.isfinite(float(pass_rate))
        or float(pass_rate) != expected_rate
    ):
        raise EvaluationPublicationError("Suite metric is inconsistent: pass_rate")
    if data.get("passed") is not all(case.get("passed") is True for case in cases):
        raise EvaluationPublicationError("Suite passed flag is inconsistent")
    for key in ("repair_attempted", "total_proposals"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EvaluationPublicationError(
                "Publication requires evidence from at least one real model proposal"
            )


def prepare_evaluation_publication(
    suite_path: Path | str,
    benchmark_root: Path | str,
    *,
    release: str,
    reviewed_by: str,
    review_note: str,
    accept_failed: bool = False,
) -> EvaluationPublication:
    """Revalidate private evidence and create a privacy-minimized publication."""

    normalized_release = _validate_release(release)
    reviewer = _required_text(reviewed_by, "Reviewer", 200)
    note = _required_text(review_note, "Review note", 2000)
    root = Path(benchmark_root).expanduser().resolve()
    try:
        benchmark = verify_frozen_benchmark(root)
        data, suite_sha256 = load_evaluation_suite(suite_path)
    except BenchmarkError as exc:
        raise EvaluationPublicationError(str(exc)) from exc
    if not benchmark.passed:
        raise EvaluationPublicationError("Frozen benchmark source tree has drifted")
    if data.get("mode") != "repair" or data.get("version") not in {5, 6}:
        raise EvaluationPublicationError("Publication requires a repair suite v5 or v6")
    if not isinstance(data.get("passed"), bool):
        raise EvaluationPublicationError("Suite passed flag is invalid")
    if not data["passed"] and not accept_failed:
        raise EvaluationPublicationError(
            "Failed suite publication requires explicit --accept-failed"
        )
    model_identity = data["model_identity"]
    if model_identity.get("digest") is None:
        raise EvaluationPublicationError("Publication requires a resolved model digest")
    runtime_identity = data["runtime_identity"]
    runner = data.get("runner")
    scope = runtime_identity.get("toolchain_scope")
    local_identity = runner == "local" and scope == "host"
    container_identity = (
        data.get("version") == 6
        and runner == "docker"
        and scope == "container"
        and isinstance(runtime_identity.get("container_image"), dict)
    )
    if not local_identity and not container_identity:
        raise EvaluationPublicationError(
            "Publication requires resolved host or pinned container execution identity"
        )
    contracts, languages, case_metadata = _current_contracts(root)
    try:
        suite_contracts = evaluation_case_identity(data)
    except BenchmarkError as exc:
        raise EvaluationPublicationError(str(exc)) from exc
    if suite_contracts != contracts:
        raise EvaluationPublicationError(
            "Suite case contracts do not match the current frozen benchmark"
        )
    expected_commands = {
        tuple(command) for command in expected_toolchain_commands(languages)
    }
    actual_toolchains = runtime_identity.get("toolchains")
    actual_commands = {tuple(item["command"]) for item in actual_toolchains}
    all_resolved = all(
        item.get("available") is True and bool(item.get("version"))
        for item in actual_toolchains
    )
    if actual_commands != expected_commands or not all_resolved:
        raise EvaluationPublicationError(
            "Suite does not contain every required resolved toolchain identity"
        )
    raw_cases = data["cases"]
    if not all(isinstance(case, dict) for case in raw_cases):
        raise EvaluationPublicationError("Suite cases are invalid")
    for case in raw_cases:
        expected_metadata = case_metadata[case["relative_root"]]
        if any(case.get(key) != value for key, value in expected_metadata.items()):
            raise EvaluationPublicationError(
                f"Suite case metadata does not match the benchmark: {case['relative_root']}"
            )
    _validate_metrics(data, raw_cases)
    case_set_bytes = json.dumps(
        sorted(contracts.items()), separators=(",", ":")
    ).encode("utf-8")
    public_cases = tuple(
        {
            "relative_root": case["relative_root"],
            "contract_sha256": case["contract_sha256"],
            "name": case.get("name"),
            "language": case.get("language"),
            "difficulty": case.get("difficulty"),
            "status": case.get("status"),
            "passed": case.get("passed"),
            "error_kind": case.get("error_kind"),
        }
        for case in sorted(raw_cases, key=lambda item: item["relative_root"])
    )
    benchmark_payload = {
        "name": benchmark.name,
        "manifest_sha256": benchmark.manifest_sha256,
        "cases": [
            {
                "path": case.relative_path,
                "source": case.source.to_dict(),
                "tree_sha256": case.expected_tree_sha256,
            }
            for case in benchmark.cases
        ],
    }
    public_runtime_identity = {
        key: runtime_identity[key]
        for key in (
            "python",
            "implementation",
            "system",
            "release",
            "machine",
            "toolchain_scope",
            "toolchains",
        )
    }
    public_runtime_identity["container_image"] = runtime_identity.get(
        "container_image"
    )
    started_at = _required_text(data.get("started_at"), "Suite start time", 100)
    try:
        parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationPublicationError("Suite start time is invalid") from exc
    if parsed_started_at.tzinfo is None:
        raise EvaluationPublicationError("Suite start time must include a timezone")
    return EvaluationPublication(
        normalized_release,
        datetime.now(timezone.utc).isoformat(),
        reviewer,
        note,
        not data["passed"],
        benchmark_payload,
        suite_sha256,
        data["version"],
        started_at,
        data["passed"],
        data["autofix_version"],
        data["model"],
        dict(model_identity),
        data["runner"],
        public_runtime_identity,
        data["max_attempts"],
        hashlib.sha256(case_set_bytes).hexdigest(),
        dict(data["metrics"]),
        public_cases,
    )


def publish_evaluation_publication(
    publication: EvaluationPublication,
    output_root: Path | str,
    *,
    expected_suite_sha256: str,
) -> Path:
    """Publish one reviewed release atomically without overwriting prior evidence."""

    if expected_suite_sha256.lower() != publication.source_suite_sha256:
        raise EvaluationPublicationError(
            "Expected suite SHA-256 does not match the reviewed source evidence"
        )
    benchmark_name = publication.benchmark["name"]
    if not isinstance(benchmark_name, str) or not _SAFE_COMPONENT.fullmatch(
        benchmark_name
    ):
        raise EvaluationPublicationError(
            "Benchmark name is unsafe for a publication path"
        )
    destination = (
        Path(output_root).expanduser().resolve()
        / benchmark_name
        / f"{publication.release}.json"
    )
    try:
        publish_json_exclusive(
            destination,
            publication.to_dict(),
            label="Evaluation publication",
        )
    except PropertyApplyPolicyError as exc:
        raise EvaluationPublicationError(str(exc)) from exc
    return destination
