from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from autofix.benchmark import (
    BenchmarkError,
    compare_evaluation_suites,
    verify_frozen_benchmark,
)
from autofix.cli import app


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = REPOSITORY_ROOT / "examples" / "complex-validation"


def _suite(path: Path, *, pass_rate: float, contract: str = "a" * 64) -> Path:
    payload = {
        "schema": "autofix-evaluation-suite",
        "version": 3,
        "mode": "repair",
        "model": "fixture-model",
        "runner": "local",
        "max_attempts": 3,
        "autofix_version": "0.19.0",
        "metrics": {
            "pass_rate": pass_rate,
            "first_attempt_pass_rate": pass_rate,
            "average_attempts": 2.0,
            "static_rejection_rate": 0.1,
            "verification_failure_rate": 0.2,
            "unexpected_change_rate": 0.0,
            "missing_required_change_rate": 0.0,
            "required_context_recall": 1.0,
            "required_context_top5_recall": 1.0,
            "context_truncation_rate": 0.0,
            "context_fallback_rate": 0.1,
        },
        "cases": [
            {
                "relative_root": "case-a",
                "contract_sha256": contract,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_repository_benchmark_manifest_matches_every_case() -> None:
    result = verify_frozen_benchmark(BENCHMARK_ROOT)

    assert result.passed
    assert [case.relative_path for case in result.cases] == [
        "go-stdlib-cutprefix",
        "node-checkout-service",
        "python-more-itertools-take",
        "python-order-service",
    ]
    assert all(case.file_count > 0 for case in result.cases)
    upstream = [case for case in result.cases if case.source.kind == "upstream"]
    assert [(case.source.repository, case.source.license) for case in upstream] == [
        ("https://github.com/golang/go", "BSD-3-Clause"),
        ("https://github.com/more-itertools/more-itertools", "MIT"),
    ]
    assert upstream[0].source.revision == "26b5783b72376acd0386f78295e678b9a6bff30e"
    assert upstream[1].source.revision == "4aeb4338ae1407d8da9723e86da99efeb446f719"


def test_benchmark_detects_source_tree_drift(tmp_path: Path) -> None:
    copied = tmp_path / "benchmark"
    shutil.copytree(BENCHMARK_ROOT, copied, ignore=shutil.ignore_patterns(".autofix"))
    target = copied / "python-order-service" / "src" / "order_service" / "pricing.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    result = verify_frozen_benchmark(copied)

    assert not result.passed
    assert [case.relative_path for case in result.cases if not case.matched] == [
        "python-order-service"
    ]


def test_benchmark_rejects_incomplete_manifest_coverage(tmp_path: Path) -> None:
    copied = tmp_path / "benchmark"
    shutil.copytree(BENCHMARK_ROOT, copied, ignore=shutil.ignore_patterns(".autofix"))
    manifest = copied / "autofix-benchmark.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["cases"] = data["cases"][:1]
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="coverage mismatch"):
        verify_frozen_benchmark(copied)


def test_benchmark_rejects_unpinned_upstream_provenance(tmp_path: Path) -> None:
    copied = tmp_path / "benchmark"
    shutil.copytree(BENCHMARK_ROOT, copied, ignore=shutil.ignore_patterns(".autofix"))
    manifest = copied / "autofix-benchmark.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["cases"][0]["source"] = {
        "kind": "upstream",
        "repository": "git://example.invalid/project",
        "revision": "main",
        "license": "MIT",
    }
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="commit hash"):
        verify_frozen_benchmark(copied)


def test_evaluation_trend_reports_directional_regressions(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=0.5)

    result = compare_evaluation_suites(current, baseline)

    assert not result.passed
    assert "pass_rate" in result.regressions
    metric = next(item for item in result.metrics if item.name == "pass_rate")
    assert metric.delta == -0.5
    assert metric.direction == "higher"
    assert result.current_identity["model"] == "fixture-model"
    assert len(result.current_sha256) == 64


def test_evaluation_trend_rejects_case_contract_drift(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=1.0, contract="b" * 64)

    with pytest.raises(BenchmarkError, match="same case contracts"):
        compare_evaluation_suites(current, baseline)


def test_evaluation_trend_rejects_model_identity_drift(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=1.0)
    current_payload = json.loads(current.read_text(encoding="utf-8"))
    current_payload["model_identity"] = {
        "name": "fixture-model",
        "digest": "b" * 64,
    }
    current.write_text(json.dumps(current_payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="model or runtime identities"):
        compare_evaluation_suites(current, baseline)


def test_evaluation_trend_rejects_invalid_v4_model_digest(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=1.0)
    for path in (baseline, current):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 4
        payload["model_identity"] = {
            "name": "fixture-model",
            "digest": "not-a-digest",
            "format": None,
            "family": None,
            "parameter_size": None,
            "quantization_level": None,
        }
        payload["runtime_identity"] = {
            "python": "3.11.0",
            "implementation": "CPython",
            "system": "Windows",
            "release": "test",
            "machine": "AMD64",
            "executable": "python.exe",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="model identity is invalid"):
        compare_evaluation_suites(current, baseline)


def test_evaluation_trend_rejects_invalid_v5_toolchain_identity(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=1.0)
    for path in (baseline, current):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 5
        payload["model_identity"] = {
            "name": "fixture-model",
            "digest": "a" * 64,
            "format": "gguf",
            "family": "fixture",
            "parameter_size": "1B",
            "quantization_level": "Q4",
        }
        payload["runtime_identity"] = {
            "python": "3.11.0",
            "implementation": "CPython",
            "system": "Windows",
            "release": "test",
            "machine": "AMD64",
            "executable": "python.exe",
            "toolchain_scope": "host",
            "toolchains": [
                {
                    "name": "go",
                    "command": [],
                    "available": True,
                    "version": "go version go1.21.2 windows/amd64",
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="runtime identity is invalid"):
        compare_evaluation_suites(current, baseline)


def test_evaluation_trend_rejects_docker_image_identity_drift(
    tmp_path: Path,
) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=1.0)
    for index, path in enumerate((baseline, current)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 6
        payload["runner"] = "docker"
        payload["model_identity"] = {
            "name": "fixture-model",
            "digest": "a" * 64,
            "format": "gguf",
            "family": "fixture",
            "parameter_size": "1B",
            "quantization_level": "Q4",
        }
        payload["runtime_identity"] = {
            "python": "3.11.0",
            "implementation": "CPython",
            "system": "Linux",
            "release": "test",
            "machine": "x86_64",
            "executable": "python",
            "toolchain_scope": "container",
            "container_image": {
                "reference": "fixture:latest",
                "image_id": "sha256:" + ("b" if index == 0 else "c") * 64,
                "repo_digests": [],
            },
            "toolchains": [
                {
                    "name": "node",
                    "command": ["node", "--version"],
                    "available": True,
                    "version": "v22.0.0",
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkError, match="model or runtime identities"):
        compare_evaluation_suites(current, baseline)


def test_evaluation_trend_rejects_non_finite_metrics(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=float("nan"))

    with pytest.raises(BenchmarkError, match="not finite"):
        compare_evaluation_suites(current, baseline)


def test_benchmark_cli_json_matches_schema() -> None:
    result = runner.invoke(
        app, ["benchmark-verify", str(BENCHMARK_ROOT), "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas" / "autofix-benchmark-verification-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(schema["required"]) == set(payload)
    assert schema["properties"]["schema"]["const"] == payload["schema"]


def test_evaluation_trend_cli_can_fail_ci_and_publish_evidence(tmp_path: Path) -> None:
    baseline = _suite(tmp_path / "baseline.json", pass_rate=1.0)
    current = _suite(tmp_path / "current.json", pass_rate=0.5)
    output = tmp_path / "trend.json"

    result = runner.invoke(
        app,
        [
            "evaluation-trend",
            str(current),
            "--baseline",
            str(baseline),
            "--output",
            str(output),
            "--fail-on-regression",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert payload == saved
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas" / "autofix-evaluation-trend-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(schema["required"]) == set(payload)
