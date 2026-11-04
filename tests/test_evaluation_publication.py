from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.cli import app
from autofix.evaluation_publication import (
    EvaluationPublicationError,
    prepare_evaluation_publication,
    publish_evaluation_publication,
)
from autofix.evaluation import load_evaluation_manifest
from autofix.evaluation_suite import discover_evaluation_cases


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_BENCHMARK = REPOSITORY_ROOT / "examples" / "complex-validation"


def _suite(
    path: Path,
    benchmark: Path,
    *,
    passed: bool = True,
    runner_name: str = "local",
) -> Path:
    cases = []
    for case_root in discover_evaluation_cases(benchmark):
        relative = case_root.relative_to(benchmark).as_posix()
        manifest = load_evaluation_manifest(case_root)
        contract = hashlib.sha256((case_root / "autofix-eval.yml").read_bytes()).hexdigest()
        cases.append(
            {
                "root": f"PRIVATE/{relative}",
                "relative_root": relative,
                "name": manifest.name,
                "language": manifest.language,
                "difficulty": manifest.difficulty,
                "tags": [],
                "contract_sha256": contract,
                "status": "passed" if passed else "failed",
                "passed": passed,
                "duration_seconds": 1.0,
                "error_kind": None,
                "error": "PRIVATE build output" if not passed else None,
                "evaluation": {"private": "PRIVATE source and logs"},
                "report_path": "PRIVATE/report.json",
            }
        )
    count = len(cases)
    passed_count = count if passed else 0
    payload = {
        "schema": "autofix-evaluation-suite",
        "version": 6,
        "mode": "repair",
        "root": "PRIVATE/root",
        "started_at": "2026-11-03T08:00:00+00:00",
        "duration_seconds": 10.0,
        "passed": passed,
        "model": "qwen2.5-coder:7b",
        "runner": runner_name,
        "max_attempts": 3,
        "autofix_version": "0.19.0",
        "model_identity": {
            "name": "qwen2.5-coder:7b",
            "digest": "a" * 64,
            "format": "gguf",
            "family": "qwen2",
            "parameter_size": "7.6B",
            "quantization_level": "Q4_K_M",
        },
        "runtime_identity": {
            "python": "3.11.4",
            "implementation": "CPython",
            "system": "Windows",
            "release": "test",
            "machine": "AMD64",
            "executable": "PRIVATE/python.exe",
            "toolchain_scope": "host" if runner_name == "local" else "container",
            "container_image": None
            if runner_name == "local"
            else {
                "reference": "autofix-eval:1",
                "image_id": "sha256:" + "b" * 64,
                "repo_digests": ["autofix-eval@sha256:" + "c" * 64],
            },
            "toolchains": [
                {
                    "name": "go",
                    "command": ["go", "version"],
                    "available": True,
                    "version": "go version go1.21.2 windows/amd64",
                },
                {
                    "name": "node",
                    "command": ["node", "--version"],
                    "available": True,
                    "version": "v24.14.0",
                },
            ],
        },
        "metrics": {
            "total": count,
            "passed": passed_count,
            "failed": count - passed_count,
            "pass_rate": 1.0 if passed else 0.0,
            "repair_attempted": count,
            "total_proposals": count + 1,
        },
        "cases": cases,
        "report_dir": "PRIVATE/report",
        "json_report_path": "PRIVATE/suite.json",
        "markdown_report_path": "PRIVATE/suite.md",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _benchmark(tmp_path: Path) -> Path:
    destination = tmp_path / "benchmark"
    shutil.copytree(SOURCE_BENCHMARK, destination, ignore=shutil.ignore_patterns(".autofix"))
    return destination


def test_review_builds_path_free_source_bound_publication(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "suite.json", benchmark)

    publication = prepare_evaluation_publication(
        suite,
        benchmark,
        release="2026.11-qwen25",
        reviewed_by="reviewer",
        review_note="Reviewed the model identity, failures, and verified diffs.",
    )
    payload = publication.to_dict()

    assert payload["suite_passed"] is True
    assert payload["source_suite_sha256"] == hashlib.sha256(suite.read_bytes()).hexdigest()
    assert len(payload["benchmark"]["cases"]) == 4
    assert [item["command"] for item in payload["runtime_identity"]["toolchains"]] == [
        ["go", "version"],
        ["node", "--version"],
    ]
    serialized = json.dumps(payload)
    assert "PRIVATE" not in serialized
    assert "executable" not in payload["runtime_identity"]
    assert "evaluation" not in payload["cases"][0]


def test_review_accepts_pinned_container_evidence(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(
        tmp_path / "docker-suite.json", benchmark, runner_name="docker"
    )

    publication = prepare_evaluation_publication(
        suite,
        benchmark,
        release="docker-reviewed-1",
        reviewed_by="reviewer",
        review_note="Reviewed the pinned image and container toolchains.",
    ).to_dict()

    assert publication["version"] == 2
    assert publication["runner"] == "docker"
    assert publication["runtime_identity"]["toolchain_scope"] == "container"
    assert publication["runtime_identity"]["container_image"]["image_id"] == (
        "sha256:" + "b" * 64
    )


def test_review_keeps_historical_v5_local_compatibility(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "local-v5.json", benchmark)
    payload = json.loads(suite.read_text(encoding="utf-8"))
    payload["version"] = 5
    payload["runtime_identity"].pop("container_image")
    suite.write_text(json.dumps(payload), encoding="utf-8")

    publication = prepare_evaluation_publication(
        suite,
        benchmark,
        release="historical-v5",
        reviewed_by="reviewer",
        review_note="Reviewed historical local evidence.",
    ).to_dict()

    assert publication["source_suite_version"] == 5
    assert publication["runtime_identity"]["container_image"] is None


def test_review_rejects_unresolved_container_toolchain(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "docker-suite.json", benchmark, runner_name="docker")
    payload = json.loads(suite.read_text(encoding="utf-8"))
    payload["runtime_identity"]["toolchains"][0]["available"] = False
    payload["runtime_identity"]["toolchains"][0]["version"] = None
    suite.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationPublicationError, match="resolved toolchain"):
        prepare_evaluation_publication(
            suite,
            benchmark,
            release="missing-container-tool",
            reviewed_by="reviewer",
            review_note="Reviewed incomplete container evidence.",
        )


def test_publication_requires_explicit_failed_acceptance(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "suite.json", benchmark, passed=False)

    with pytest.raises(EvaluationPublicationError, match="--accept-failed"):
        prepare_evaluation_publication(
            suite,
            benchmark,
            release="failed-run",
            reviewed_by="reviewer",
            review_note="Reviewed failure evidence.",
        )

    publication = prepare_evaluation_publication(
        suite,
        benchmark,
        release="failed-run",
        reviewed_by="reviewer",
        review_note="Reviewed failure evidence.",
        accept_failed=True,
    )
    assert publication.accepted_failed_suite


def test_publish_requires_reviewed_digest_and_refuses_overwrite(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "suite.json", benchmark)
    publication = prepare_evaluation_publication(
        suite,
        benchmark,
        release="release-1",
        reviewed_by="reviewer",
        review_note="Reviewed all public fields.",
    )

    with pytest.raises(EvaluationPublicationError, match="does not match"):
        publish_evaluation_publication(
            publication, tmp_path / "published", expected_suite_sha256="b" * 64
        )
    destination = publish_evaluation_publication(
        publication,
        tmp_path / "published",
        expected_suite_sha256=publication.source_suite_sha256,
    )
    assert destination.name == "release-1.json"
    with pytest.raises(EvaluationPublicationError, match="already exists"):
        publish_evaluation_publication(
            publication,
            tmp_path / "published",
            expected_suite_sha256=publication.source_suite_sha256,
        )


def test_publication_rejects_contract_drift(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "suite.json", benchmark)
    payload = json.loads(suite.read_text(encoding="utf-8"))
    payload["cases"][0]["contract_sha256"] = "b" * 64
    suite.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationPublicationError, match="case contracts"):
        prepare_evaluation_publication(
            suite,
            benchmark,
            release="drifted",
            reviewed_by="reviewer",
            review_note="Reviewed drifted evidence.",
        )


def test_publication_cli_is_preview_first_and_json_matches_schema(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path)
    suite = _suite(tmp_path / "suite.json", benchmark)
    arguments = [
        "evaluation-publish",
        str(suite),
        "--benchmark",
        str(benchmark),
        "--release",
        "reviewed-1",
        "--reviewed-by",
        "reviewer",
        "--review-note",
        "Reviewed model and build evidence.",
        "--json",
    ]

    preview = runner.invoke(app, arguments)

    assert preview.exit_code == 0, preview.stdout
    payload = json.loads(preview.stdout)
    assert not (tmp_path / "published").exists()
    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "autofix-evaluation-publication-v2.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(schema["required"]) == set(payload)

    published = runner.invoke(
        app,
        arguments[:-1]
        + [
            "--publish",
            "--expected-suite-sha256",
            payload["source_suite_sha256"],
            "--output-root",
            str(tmp_path / "published"),
            "--json",
        ],
    )
    assert published.exit_code == 0, published.stdout
    assert (
        tmp_path
        / "published"
        / "autofix-complex-boundary-v1"
        / "reviewed-1.json"
    ).is_file()
