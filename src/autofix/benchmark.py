"""Frozen evaluation-corpus verification and comparable suite trends."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from autofix.evaluation_suite import discover_evaluation_cases
from autofix.property_apply import PropertyApplyPolicyError, publish_json_exclusive


BENCHMARK_MANIFEST = "autofix-benchmark.yml"
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
MAX_CASE_FILES = 10_000
MAX_CASE_BYTES = 64 * 1024 * 1024


class BenchmarkError(ValueError):
    """Raised when frozen corpus or suite comparison evidence is invalid."""


@dataclass(frozen=True)
class BenchmarkSource:
    """Auditable origin metadata for one frozen evaluation case."""

    kind: str
    repository: str
    revision: str
    license: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "repository": self.repository,
            "revision": self.revision,
            "license": self.license,
        }


@dataclass(frozen=True)
class FrozenBenchmarkCase:
    """One expected corpus path, provenance identity, and content digest."""

    relative_path: str
    source: BenchmarkSource
    expected_tree_sha256: str
    actual_tree_sha256: str
    file_count: int
    size_bytes: int

    @property
    def matched(self) -> bool:
        return self.expected_tree_sha256 == self.actual_tree_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "source": self.source.to_dict(),
            "expected_tree_sha256": self.expected_tree_sha256,
            "actual_tree_sha256": self.actual_tree_sha256,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class BenchmarkVerificationResult:
    """Verification result for a complete declared frozen benchmark corpus."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    name: str
    cases: tuple[FrozenBenchmarkCase, ...]
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        return bool(self.cases) and all(case.matched for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-benchmark-verification",
            "version": 1,
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "name": self.name,
            "passed": self.passed,
            "cases": [case.to_dict() for case in self.cases],
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class TrendMetric:
    """One comparable numeric metric and its signed current-minus-baseline delta."""

    name: str
    baseline: float
    current: float
    delta: float
    direction: str
    regressed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "baseline": self.baseline,
            "current": self.current,
            "delta": self.delta,
            "direction": self.direction,
            "regressed": self.regressed,
        }


@dataclass(frozen=True)
class EvaluationTrendResult:
    """Strict comparison of two evaluation suites over the same case contracts."""

    baseline_path: Path
    current_path: Path
    baseline_sha256: str
    current_sha256: str
    mode: str
    case_set_sha256: str
    baseline_identity: dict[str, Any]
    current_identity: dict[str, Any]
    metrics: tuple[TrendMetric, ...]
    report_path: Path | None = None

    @property
    def regressions(self) -> tuple[str, ...]:
        return tuple(metric.name for metric in self.metrics if metric.regressed)

    @property
    def passed(self) -> bool:
        return not self.regressions

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "autofix-evaluation-trend",
            "version": 1,
            "baseline_path": str(self.baseline_path),
            "current_path": str(self.current_path),
            "baseline_sha256": self.baseline_sha256,
            "current_sha256": self.current_sha256,
            "mode": self.mode,
            "case_set_sha256": self.case_set_sha256,
            "baseline_identity": self.baseline_identity,
            "current_identity": self.current_identity,
            "passed": self.passed,
            "regressions": list(self.regressions),
            "metrics": [metric.to_dict() for metric in self.metrics],
            "report_path": str(self.report_path) if self.report_path else None,
        }


def _safe_relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BenchmarkError("Benchmark case path must be a non-empty string")
    relative = Path(raw.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise BenchmarkError(f"Unsafe benchmark case path: {raw}")
    return relative.as_posix()


def _source(raw: Any) -> BenchmarkSource:
    required = {"kind", "repository", "revision", "license"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise BenchmarkError("Benchmark source must contain exact provenance fields")
    if raw["kind"] not in {"upstream", "synthetic"}:
        raise BenchmarkError("Benchmark source kind must be upstream or synthetic")
    if not all(isinstance(raw[key], str) and raw[key].strip() for key in required):
        raise BenchmarkError("Benchmark source provenance values cannot be empty")
    if raw["kind"] == "upstream":
        revision = raw["revision"].lower()
        if (
            not raw["repository"].startswith("https://")
            or not 7 <= len(revision) <= 64
            or any(char not in "0123456789abcdef" for char in revision)
        ):
            raise BenchmarkError(
                "Upstream benchmark source requires an HTTPS repository and commit hash"
            )
    elif not raw["repository"].startswith("local://"):
        raise BenchmarkError("Synthetic benchmark source must use a local:// identity")
    return BenchmarkSource(
        raw["kind"], raw["repository"], raw["revision"], raw["license"]
    )


def hash_benchmark_tree(root: Path) -> tuple[str, int, int]:
    """Hash regular case files by normalized path and bytes with bounded traversal."""

    digest = hashlib.sha256()
    file_count = 0
    size_bytes = 0
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        kept_names: list[str] = []
        for name in sorted(names):
            child = directory_path / name
            if name in _IGNORED_DIRECTORIES:
                continue
            if child.is_symlink():
                raise BenchmarkError(f"Benchmark contains a directory symlink: {child}")
            kept_names.append(name)
        names[:] = kept_names
        for name in sorted(files):
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                raise BenchmarkError(f"Benchmark contains an unsafe file: {path}")
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            file_count += 1
            size_bytes += len(content)
            if file_count > MAX_CASE_FILES or size_bytes > MAX_CASE_BYTES:
                raise BenchmarkError("Benchmark case exceeds the frozen corpus limits")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    if not file_count:
        raise BenchmarkError(f"Benchmark case contains no files: {root}")
    return digest.hexdigest(), file_count, size_bytes


def verify_frozen_benchmark(root: Path | str) -> BenchmarkVerificationResult:
    """Verify strict manifest structure, complete case coverage, and tree digests."""

    benchmark_root = Path(root).expanduser().resolve()
    manifest_path = benchmark_root / BENCHMARK_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BenchmarkError(f"Missing regular {BENCHMARK_MANIFEST}: {benchmark_root}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        data = yaml.safe_load(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BenchmarkError("Benchmark manifest must be valid UTF-8 YAML") from exc
    if not isinstance(data, dict) or set(data) != {"version", "name", "cases"}:
        raise BenchmarkError("Benchmark manifest has an unsupported structure")
    if data["version"] != 1 or not isinstance(data["name"], str) or not data["name"].strip():
        raise BenchmarkError("Benchmark manifest version or name is invalid")
    raw_cases = data["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise BenchmarkError("Benchmark manifest must declare at least one case")
    declared: dict[str, tuple[BenchmarkSource, str]] = {}
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {"path", "source", "tree_sha256"}:
            raise BenchmarkError("Benchmark case has an unsupported structure")
        relative = _safe_relative_path(raw["path"])
        expected = raw["tree_sha256"]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            raise BenchmarkError(f"Benchmark tree SHA-256 is invalid: {relative}")
        if relative in declared:
            raise BenchmarkError(f"Duplicate benchmark case path: {relative}")
        declared[relative] = (_source(raw["source"]), expected)
    discovered = {
        path.relative_to(benchmark_root).as_posix()
        for path in discover_evaluation_cases(benchmark_root)
    }
    if discovered != set(declared):
        missing = sorted(discovered - set(declared))
        extra = sorted(set(declared) - discovered)
        raise BenchmarkError(
            f"Benchmark manifest coverage mismatch; undeclared={missing}, missing={extra}"
        )
    cases: list[FrozenBenchmarkCase] = []
    for relative in sorted(declared):
        source, expected = declared[relative]
        actual, count, size = hash_benchmark_tree(benchmark_root / relative)
        cases.append(
            FrozenBenchmarkCase(relative, source, expected, actual, count, size)
        )
    return BenchmarkVerificationResult(
        benchmark_root,
        manifest_path,
        hashlib.sha256(manifest_bytes).hexdigest(),
        data["name"],
        tuple(cases),
    )


_METRIC_DIRECTIONS = {
    "pass_rate": "higher",
    "first_attempt_pass_rate": "higher",
    "average_attempts": "lower",
    "static_rejection_rate": "lower",
    "verification_failure_rate": "lower",
    "unexpected_change_rate": "lower",
    "missing_required_change_rate": "lower",
    "required_context_recall": "higher",
    "required_context_top5_recall": "higher",
    "context_truncation_rate": "lower",
    "context_fallback_rate": "lower",
}


def load_evaluation_suite(path: Path | str) -> tuple[dict[str, Any], str]:
    """Load supported suite evidence and validate its execution identity."""

    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise BenchmarkError(f"Evaluation suite must be a regular JSON file: {path}")
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Evaluation suite is invalid JSON: {path}") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != "autofix-evaluation-suite"
        or data.get("mode") not in {"baseline", "repair"}
        or not isinstance(data.get("metrics"), dict)
        or not isinstance(data.get("cases"), list)
        or not data["cases"]
    ):
        raise BenchmarkError(f"Evaluation suite has an unsupported structure: {path}")
    expected_version = 1 if data["mode"] == "baseline" else data.get("version")
    if expected_version not in (
        {1} if data["mode"] == "baseline" else {2, 3, 4, 5}
    ):
        raise BenchmarkError(f"Evaluation suite version is unsupported: {path}")
    if data["mode"] == "repair" and (
        not isinstance(data.get("model"), str)
        or not data["model"].strip()
        or data.get("runner") not in {"local", "docker"}
        or isinstance(data.get("max_attempts"), bool)
        or not isinstance(data.get("max_attempts"), int)
        or not 1 <= data["max_attempts"] <= 20
        or not isinstance(data.get("autofix_version"), str)
        or not data["autofix_version"].strip()
    ):
        raise BenchmarkError(f"Repair suite execution identity is invalid: {path}")
    if data["mode"] == "repair" and data.get("version") in {4, 5}:
        model_identity = data.get("model_identity")
        runtime_identity = data.get("runtime_identity")
        model_keys = {
            "name",
            "digest",
            "format",
            "family",
            "parameter_size",
            "quantization_level",
        }
        runtime_keys = {
            "python",
            "implementation",
            "system",
            "release",
            "machine",
            "executable",
        }
        runtime_text_keys = set(runtime_keys)
        if data["version"] == 5:
            runtime_keys.update({"toolchain_scope", "toolchains"})
        if (
            not isinstance(model_identity, dict)
            or set(model_identity) != model_keys
            or not isinstance(model_identity.get("name"), str)
            or not model_identity["name"].strip()
            or any(
                model_identity[key] is not None
                and not isinstance(model_identity[key], str)
                for key in model_keys - {"name", "digest"}
            )
        ):
            raise BenchmarkError(f"Repair suite model identity is invalid: {path}")
        digest = model_identity.get("digest")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise BenchmarkError(f"Repair suite model identity is invalid: {path}")
        if (
            not isinstance(runtime_identity, dict)
            or set(runtime_identity) != runtime_keys
            or any(
                not isinstance(runtime_identity[key], str)
                for key in runtime_text_keys
            )
            or any(
                not runtime_identity[key]
                for key in {"python", "implementation", "system", "executable"}
            )
        ):
            raise BenchmarkError(f"Repair suite runtime identity is invalid: {path}")
        if data["version"] == 5:
            if runtime_identity["toolchain_scope"] not in {
                "host",
                "container-unresolved",
            }:
                raise BenchmarkError(
                    f"Repair suite runtime identity is invalid: {path}"
                )
            toolchains = runtime_identity["toolchains"]
            if not isinstance(toolchains, list):
                raise BenchmarkError(
                    f"Repair suite runtime identity is invalid: {path}"
                )
            seen_toolchains: set[str] = set()
            for toolchain in toolchains:
                if (
                    not isinstance(toolchain, dict)
                    or set(toolchain)
                    != {"name", "command", "available", "version"}
                    or not isinstance(toolchain.get("name"), str)
                    or not toolchain["name"]
                    or toolchain["name"] in seen_toolchains
                    or not isinstance(toolchain.get("command"), list)
                    or not toolchain["command"]
                    or any(
                        not isinstance(argument, str) or not argument
                        for argument in toolchain["command"]
                    )
                    or not isinstance(toolchain.get("available"), bool)
                    or (
                        toolchain.get("version") is not None
                        and (
                            not isinstance(toolchain["version"], str)
                            or len(toolchain["version"]) > 500
                        )
                    )
                    or (
                        not toolchain["available"]
                        and toolchain.get("version") is not None
                    )
                ):
                    raise BenchmarkError(
                        f"Repair suite runtime identity is invalid: {path}"
                    )
                seen_toolchains.add(toolchain["name"])
            if (
                runtime_identity["toolchain_scope"] == "container-unresolved"
                and toolchains
            ):
                raise BenchmarkError(
                    f"Repair suite runtime identity is invalid: {path}"
                )
    return data, hashlib.sha256(raw).hexdigest()


def evaluation_case_identity(data: dict[str, Any]) -> dict[str, str]:
    """Return the unique relative-path to contract-digest suite identity."""

    identity: dict[str, str] = {}
    for case in data["cases"]:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("relative_root"), str)
            or not isinstance(case.get("contract_sha256"), str)
            or len(case["contract_sha256"]) != 64
            or any(
                char not in "0123456789abcdef"
                for char in case["contract_sha256"]
            )
        ):
            raise BenchmarkError("Evaluation suite case identity is invalid")
        relative = case["relative_root"]
        if relative in identity:
            raise BenchmarkError(f"Duplicate evaluation case identity: {relative}")
        identity[relative] = case["contract_sha256"]
    return identity


def _run_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Preserve available execution identity without making models comparable by fiat."""

    return {
        "model": data.get("model"),
        "model_identity": data.get("model_identity"),
        "runner": data.get("runner"),
        "runtime_identity": data.get("runtime_identity"),
        "max_attempts": data.get("max_attempts"),
        "autofix_version": data.get("autofix_version"),
    }


def compare_evaluation_suites(
    current: Path | str, baseline: Path | str
) -> EvaluationTrendResult:
    """Compare stable metrics only when mode and exact case contracts match."""

    current_path = Path(current).expanduser().resolve()
    baseline_path = Path(baseline).expanduser().resolve()
    current_data, current_sha256 = load_evaluation_suite(current_path)
    baseline_data, baseline_sha256 = load_evaluation_suite(baseline_path)
    if current_data["mode"] != baseline_data["mode"]:
        raise BenchmarkError("Evaluation suites use different modes")
    current_cases = evaluation_case_identity(current_data)
    baseline_cases = evaluation_case_identity(baseline_data)
    if current_cases != baseline_cases:
        raise BenchmarkError("Evaluation suites do not contain the same case contracts")
    baseline_identity = _run_identity(baseline_data)
    current_identity = _run_identity(current_data)
    comparable_identity_keys = (
        "model",
        "model_identity",
        "runner",
        "runtime_identity",
        "max_attempts",
    )
    if any(
        baseline_identity[key] != current_identity[key]
        for key in comparable_identity_keys
    ):
        raise BenchmarkError(
            "Evaluation suites use different model or runtime identities"
        )
    identity_bytes = json.dumps(
        sorted(current_cases.items()), separators=(",", ":")
    ).encode("utf-8")
    current_metrics = current_data["metrics"]
    baseline_metrics = baseline_data["metrics"]
    metrics: list[TrendMetric] = []
    for name, direction in _METRIC_DIRECTIONS.items():
        if name not in current_metrics or name not in baseline_metrics:
            continue
        current_value = current_metrics[name]
        baseline_value = baseline_metrics[name]
        if (
            isinstance(current_value, bool)
            or isinstance(baseline_value, bool)
            or not isinstance(current_value, (int, float))
            or not isinstance(baseline_value, (int, float))
        ):
            raise BenchmarkError(f"Evaluation metric is not numeric: {name}")
        if not math.isfinite(float(current_value)) or not math.isfinite(
            float(baseline_value)
        ):
            raise BenchmarkError(f"Evaluation metric is not finite: {name}")
        values = (float(current_value), float(baseline_value))
        if name == "average_attempts":
            if any(value < 0 for value in values):
                raise BenchmarkError(f"Evaluation metric is out of range: {name}")
        elif any(value < 0 or value > 1 for value in values):
            raise BenchmarkError(f"Evaluation metric is out of range: {name}")
        delta = round(float(current_value) - float(baseline_value), 6)
        regressed = delta < 0 if direction == "higher" else delta > 0
        metrics.append(
            TrendMetric(
                name,
                float(baseline_value),
                float(current_value),
                delta,
                direction,
                regressed,
            )
        )
    if not metrics:
        raise BenchmarkError("Evaluation suites have no comparable metrics")
    return EvaluationTrendResult(
        baseline_path,
        current_path,
        baseline_sha256,
        current_sha256,
        current_data["mode"],
        hashlib.sha256(identity_bytes).hexdigest(),
        baseline_identity,
        current_identity,
        tuple(metrics),
    )


def save_benchmark_json(
    result: BenchmarkVerificationResult | EvaluationTrendResult,
    destination: Path | str,
) -> BenchmarkVerificationResult | EvaluationTrendResult:
    """Publish benchmark evidence to one caller-selected non-existing path."""

    output = Path(destination).expanduser().resolve()
    updated = replace(result, report_path=output)
    try:
        publish_json_exclusive(
            output, updated.to_dict(), label="Benchmark report"
        )
    except PropertyApplyPolicyError as exc:
        raise BenchmarkError(str(exc)) from exc
    return updated
