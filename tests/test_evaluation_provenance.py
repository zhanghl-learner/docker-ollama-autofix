from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autofix.cli import app
from autofix.evaluation_provenance import (
    EvaluationProvenanceError,
    EvaluationProvenanceVerification,
    verify_github_evaluation_provenance,
)


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIGEST = "a" * 40
ARTIFACT_REPOSITORY = "zhanghl-learner/docker-ollama-autofix"
SIGNER_WORKFLOW = (
    "zhanghl-learner/docker-ollama-autofix/.github/workflows/release.yml"
)
SOURCE_REF = "refs/tags/v0.19.0"


def _publication(path: Path) -> tuple[Path, str]:
    payload = {
        "schema": "autofix-evaluation-publication",
        "version": 2,
        "release": "2026.11-qwen25",
        "reviewed_at": "2026-11-11T00:00:00+00:00",
        "reviewed_by": "reviewer",
        "review_note": "reviewed",
        "accepted_failed_suite": False,
        "benchmark": {},
        "source_suite_sha256": "b" * 64,
        "source_suite_version": 6,
        "suite_started_at": "2026-11-11T00:00:00+00:00",
        "suite_passed": True,
        "autofix_version": "0.19.0",
        "model": "qwen2.5-coder:7b",
        "model_identity": {},
        "runner": "docker",
        "runtime_identity": {},
        "max_attempts": 3,
        "case_set_sha256": "c" * 64,
        "metrics": {},
        "cases": [],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _verification_json(digest: str) -> str:
    return json.dumps(
        [
            {
                "attestation": {"bundle": "not persisted by AutoFix"},
                "verificationResult": {
                    "statement": {
                        "predicateType": "https://slsa.dev/provenance/v1",
                        "subject": [
                            {"name": "publication.json", "digest": {"sha256": digest}}
                        ],
                        "predicate": {},
                    },
                    "signature": {"certificate": {}},
                    "verifiedTimestamps": [],
                },
            }
        ]
    )


class FakeGitHubCLI:
    def __init__(self, digest: str, *, verify_code: int = 0) -> None:
        self.digest = digest
        self.verify_code = verify_code
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        assert kwargs["shell"] is False
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "gh version 2.82.1\n", "")
        if self.verify_code:
            return subprocess.CompletedProcess(command, self.verify_code, "", "verification rejected")
        return subprocess.CompletedProcess(command, 0, _verification_json(self.digest), "")


def _verify(
    publication: Path,
    digest: str,
    fake: FakeGitHubCLI,
    **kwargs: object,
) -> EvaluationProvenanceVerification:
    return verify_github_evaluation_provenance(
        publication,
        repository=ARTIFACT_REPOSITORY,
        signer_workflow=SIGNER_WORKFLOW,
        source_digest=SOURCE_DIGEST,
        source_ref=SOURCE_REF,
        expected_publication_sha256=digest,
        command_runner=fake,
        gh_executable="gh-test",
        **kwargs,
    )


def test_online_verification_binds_all_exact_policy_flags(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    fake = FakeGitHubCLI(digest)

    result = _verify(publication, digest, fake)

    assert result.verification_mode == "online-api"
    assert result.artifact_sha256 == digest
    assert result.attestation_count == 1
    command = fake.calls[1]
    assert command[:4] == ["gh-test", "attestation", "verify", str(publication.resolve())]
    expected_pairs = {
        "--repo": ARTIFACT_REPOSITORY,
        "--signer-workflow": SIGNER_WORKFLOW,
        "--source-digest": SOURCE_DIGEST,
        "--source-ref": SOURCE_REF,
        "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--predicate-type": "https://slsa.dev/provenance/v1",
        "--digest-alg": "sha256",
        "--format": "json",
    }
    for flag, value in expected_pairs.items():
        assert command[command.index(flag) + 1] == value
    assert "--deny-self-hosted-runners" in command


def test_offline_verification_binds_bundle_root_digests_and_receipt(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    bundle = tmp_path / "bundle.jsonl"
    root = tmp_path / "trusted_root.jsonl"
    output = tmp_path / "receipt.json"
    bundle.write_text("bundle\n", encoding="utf-8")
    root.write_text("root\n", encoding="utf-8")
    fake = FakeGitHubCLI(digest)

    result = _verify(
        publication,
        digest,
        fake,
        bundle_path=bundle,
        trusted_root_path=root,
        output_path=output,
    )

    assert result.verification_mode == "offline-bundle"
    assert result.bundle_sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert result.trusted_root_sha256 == hashlib.sha256(root.read_bytes()).hexdigest()
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt == result.to_dict()
    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "autofix-evaluation-provenance-verification-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(schema["required"]) == set(receipt)
    command = fake.calls[1]
    assert command[command.index("--bundle") + 1] == str(bundle.resolve())
    assert command[command.index("--custom-trusted-root") + 1] == str(root.resolve())


def test_digest_mismatch_stops_before_private_or_networked_verification(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    fake = FakeGitHubCLI(digest)

    with pytest.raises(EvaluationProvenanceError, match="Expected publication"):
        _verify(publication, "0" * 64, fake)

    assert fake.calls == []


@pytest.mark.parametrize(
    ("repository", "workflow", "source_ref"),
    [
        ("owner-only", SIGNER_WORKFLOW, SOURCE_REF),
        (ARTIFACT_REPOSITORY, "owner/repo/release.yml", SOURCE_REF),
        (ARTIFACT_REPOSITORY, SIGNER_WORKFLOW, "main"),
    ],
)
def test_ambiguous_platform_identity_is_rejected(
    tmp_path: Path, repository: str, workflow: str, source_ref: str
) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    with pytest.raises(EvaluationProvenanceError):
        verify_github_evaluation_provenance(
            publication,
            repository=repository,
            signer_workflow=workflow,
            source_digest=SOURCE_DIGEST,
            source_ref=source_ref,
            expected_publication_sha256=digest,
            gh_executable="gh-test",
        )


def test_offline_bundle_requires_out_of_band_trusted_root(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("bundle\n", encoding="utf-8")
    fake = FakeGitHubCLI(digest)
    with pytest.raises(EvaluationProvenanceError, match="both bundle and trusted root"):
        _verify(publication, digest, fake, bundle_path=bundle)
    assert fake.calls == []


def test_subject_digest_is_rechecked_after_gh_success(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    fake = FakeGitHubCLI("0" * 64)
    with pytest.raises(EvaluationProvenanceError, match="subject"):
        _verify(publication, digest, fake)


def test_failed_gh_verification_does_not_publish_receipt(tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")
    output = tmp_path / "receipt.json"
    fake = FakeGitHubCLI(digest, verify_code=1)
    with pytest.raises(EvaluationProvenanceError, match="verification failed"):
        _verify(publication, digest, fake, output_path=output)
    assert not output.exists()


def test_cli_emits_machine_readable_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    publication, digest = _publication(tmp_path / "publication.json")

    def fail(*args: object, **kwargs: object) -> None:
        raise EvaluationProvenanceError("attestation rejected")

    monkeypatch.setattr("autofix.cli.verify_github_evaluation_provenance", fail)
    result = runner.invoke(
        app,
        [
            "evaluation-verify-provenance",
            str(publication),
            "--repo", ARTIFACT_REPOSITORY,
            "--signer-workflow", SIGNER_WORKFLOW,
            "--source-digest", SOURCE_DIGEST,
            "--source-ref", SOURCE_REF,
            "--expected-publication-sha256", digest,
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["verified"] is False
