from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from autofix.cli import app
from autofix.evaluation_signature import (
    EvaluationSignatureError,
    sign_evaluation_publication,
    verify_evaluation_signature,
)


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _keys(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_path = root / "signing-key.pem"
    public_path = root / "trusted-key.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _publication(path: Path) -> Path:
    payload = {
        "schema": "autofix-evaluation-publication",
        "version": 2,
        "release": "2026.11-signed",
        "reviewed_at": "2026-11-04T08:00:00+00:00",
        "reviewed_by": "reviewer",
        "review_note": "Reviewed frozen evidence.",
        "accepted_failed_suite": False,
        "benchmark": {},
        "source_suite_sha256": "a" * 64,
        "source_suite_version": 6,
        "suite_started_at": "2026-11-04T07:00:00+00:00",
        "suite_passed": True,
        "autofix_version": "0.19.0",
        "model": "fixture-model",
        "model_identity": {},
        "runner": "local",
        "runtime_identity": {},
        "max_attempts": 3,
        "case_set_sha256": "b" * 64,
        "metrics": {},
        "cases": [],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def test_sign_and_verify_exact_publication_bytes(tmp_path: Path) -> None:
    publication = _publication(tmp_path / "publication.json")
    private_key, public_key = _keys(tmp_path)
    signature = tmp_path / "publication.signature.json"
    digest = hashlib.sha256(publication.read_bytes()).hexdigest()

    destination = sign_evaluation_publication(
        publication,
        private_key,
        signature,
        signer="release-reviewer",
        expected_publication_sha256=digest,
    )
    verification = verify_evaluation_signature(
        publication, signature, public_key
    ).to_dict()

    assert destination == signature.resolve()
    assert verification["verified"] is True
    assert verification["publication_sha256"] == digest
    assert verification["release"] == "2026.11-signed"
    evidence = json.loads(signature.read_text(encoding="utf-8"))
    signature_schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "autofix-evaluation-signature-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    verification_schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "autofix-evaluation-signature-verification-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(signature_schema["required"]) == set(evidence)
    assert set(verification_schema["required"]) == set(verification)


def test_verification_rejects_publication_and_signed_metadata_tampering(
    tmp_path: Path,
) -> None:
    publication = _publication(tmp_path / "publication.json")
    private_key, public_key = _keys(tmp_path)
    signature = tmp_path / "publication.signature.json"
    sign_evaluation_publication(
        publication,
        private_key,
        signature,
        signer="release-reviewer",
        expected_publication_sha256=hashlib.sha256(publication.read_bytes()).hexdigest(),
    )

    publication.write_text(
        publication.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )
    with pytest.raises(EvaluationSignatureError, match="Publication bytes"):
        verify_evaluation_signature(publication, signature, public_key)

    publication = _publication(publication)
    evidence = json.loads(signature.read_text(encoding="utf-8"))
    evidence["signer"] = "attacker"
    signature.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(EvaluationSignatureError, match="verification failed"):
        verify_evaluation_signature(publication, signature, public_key)


def test_signing_requires_reviewed_digest_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    publication = _publication(tmp_path / "publication.json")
    private_key, _ = _keys(tmp_path)
    signature = tmp_path / "publication.signature.json"

    with pytest.raises(EvaluationSignatureError, match="does not match"):
        sign_evaluation_publication(
            publication,
            private_key,
            signature,
            signer="reviewer",
            expected_publication_sha256="c" * 64,
        )
    digest = hashlib.sha256(publication.read_bytes()).hexdigest()
    sign_evaluation_publication(
        publication,
        private_key,
        signature,
        signer="reviewer",
        expected_publication_sha256=digest,
    )
    with pytest.raises(EvaluationSignatureError, match="already exists"):
        sign_evaluation_publication(
            publication,
            private_key,
            signature,
            signer="reviewer",
            expected_publication_sha256=digest,
        )


def test_verification_requires_the_explicit_trusted_public_key(
    tmp_path: Path,
) -> None:
    publication = _publication(tmp_path / "publication.json")
    private_key, _ = _keys(tmp_path)
    _, wrong_public_key = _keys(tmp_path / "other")
    signature = tmp_path / "publication.signature.json"
    sign_evaluation_publication(
        publication,
        private_key,
        signature,
        signer="reviewer",
        expected_publication_sha256=hashlib.sha256(publication.read_bytes()).hexdigest(),
    )

    with pytest.raises(EvaluationSignatureError, match="fingerprint"):
        verify_evaluation_signature(publication, signature, wrong_public_key)


def test_signature_cli_round_trip_and_failure_exit(tmp_path: Path) -> None:
    publication = _publication(tmp_path / "publication.json")
    private_key, public_key = _keys(tmp_path)
    signature = tmp_path / "publication.signature.json"
    digest = hashlib.sha256(publication.read_bytes()).hexdigest()

    signed = runner.invoke(
        app,
        [
            "evaluation-sign",
            str(publication),
            "--private-key",
            str(private_key),
            "--output",
            str(signature),
            "--signer",
            "release-reviewer",
            "--expected-publication-sha256",
            digest,
        ],
    )
    verified = runner.invoke(
        app,
        [
            "evaluation-verify-signature",
            str(publication),
            str(signature),
            "--public-key",
            str(public_key),
            "--json",
        ],
    )

    assert signed.exit_code == 0, signed.stdout
    assert verified.exit_code == 0, verified.stdout
    assert json.loads(verified.stdout)["verified"] is True

    evidence = json.loads(signature.read_text(encoding="utf-8"))
    evidence["release"] = "tampered"
    signature.write_text(json.dumps(evidence), encoding="utf-8")
    failed = runner.invoke(
        app,
        [
            "evaluation-verify-signature",
            str(publication),
            str(signature),
            "--public-key",
            str(public_key),
            "--json",
        ],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["verified"] is False
