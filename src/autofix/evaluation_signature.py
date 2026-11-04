"""Ed25519 signing and verification for immutable evaluation publications."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from autofix.property_apply import PropertyApplyPolicyError, publish_json_exclusive


SIGNATURE_SCHEMA = "autofix-evaluation-signature"
SIGNATURE_VERSION = 1
VERIFICATION_SCHEMA = "autofix-evaluation-signature-verification"
SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_NAMESPACE = "autofix-evaluation-signature-v1"
_MAX_PUBLICATION_BYTES = 4 * 1024 * 1024
_MAX_KEY_BYTES = 64 * 1024
_SAFE_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PUBLICATION_FIELDS = {
    "schema",
    "version",
    "release",
    "reviewed_at",
    "reviewed_by",
    "review_note",
    "accepted_failed_suite",
    "benchmark",
    "source_suite_sha256",
    "source_suite_version",
    "suite_started_at",
    "suite_passed",
    "autofix_version",
    "model",
    "model_identity",
    "runner",
    "runtime_identity",
    "max_attempts",
    "case_set_sha256",
    "metrics",
    "cases",
}
_SIGNATURE_FIELDS = {
    "schema",
    "version",
    "algorithm",
    "namespace",
    "publication_sha256",
    "publication_schema",
    "publication_version",
    "release",
    "signed_at",
    "signer",
    "public_key_sha256",
    "signature",
}


class EvaluationSignatureError(ValueError):
    """Raised when signing input or signature evidence cannot be trusted."""


@dataclass(frozen=True)
class EvaluationSignatureVerification:
    """Successful cryptographic verification result for automation."""

    publication_sha256: str
    publication_version: int
    release: str
    signer: str
    public_key_sha256: str
    signed_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a stable verification envelope without local paths."""

        return {
            "schema": VERIFICATION_SCHEMA,
            "version": 1,
            "verified": True,
            "algorithm": SIGNATURE_ALGORITHM,
            "namespace": SIGNATURE_NAMESPACE,
            "publication_sha256": self.publication_sha256,
            "publication_version": self.publication_version,
            "release": self.release,
            "signer": self.signer,
            "public_key_sha256": self.public_key_sha256,
            "signed_at": self.signed_at,
        }


def _regular_bytes(path: Path | str, *, label: str, limit: int) -> bytes:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise EvaluationSignatureError(f"{label} must be a regular non-symlink file")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise EvaluationSignatureError(f"{label} must be a regular non-symlink file")
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > limit:
            raise EvaluationSignatureError(
                f"{label} must contain between 1 and {limit} bytes"
            )
        return resolved.read_bytes()
    except OSError as exc:
        raise EvaluationSignatureError(f"Unable to read {label.lower()}") from exc


def _publication(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationSignatureError("Publication must be valid UTF-8 JSON") from exc
    if (
        not isinstance(data, dict)
        or set(data) != _PUBLICATION_FIELDS
        or data.get("schema") != "autofix-evaluation-publication"
        or data.get("version") not in {1, 2}
        or not isinstance(data.get("release"), str)
        or _SAFE_RELEASE.fullmatch(data["release"]) is None
        or not isinstance(data.get("source_suite_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", data["source_suite_sha256"]) is None
        or not isinstance(data.get("case_set_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", data["case_set_sha256"]) is None
        or data.get("runner") not in {"local", "docker"}
        or not isinstance(data.get("benchmark"), dict)
        or not isinstance(data.get("model_identity"), dict)
        or not isinstance(data.get("runtime_identity"), dict)
        or not isinstance(data.get("metrics"), dict)
        or not isinstance(data.get("cases"), list)
    ):
        raise EvaluationSignatureError("Publication has an unsupported identity")
    return data


def _message(statement: dict[str, Any]) -> bytes:
    canonical = json.dumps(
        statement,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SIGNATURE_NAMESPACE.encode("ascii") + b"\0" + canonical


def _public_fingerprint(public_key: Ed25519PublicKey) -> str:
    encoded = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: Any, label: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise EvaluationSignatureError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    return normalized


def sign_evaluation_publication(
    publication_path: Path | str,
    private_key_path: Path | str,
    output_path: Path | str,
    *,
    signer: str,
    expected_publication_sha256: str,
) -> Path:
    """Sign exact publication bytes and atomically publish detached evidence."""

    raw = _regular_bytes(
        publication_path, label="Publication", limit=_MAX_PUBLICATION_BYTES
    )
    publication = _publication(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_publication_sha256.lower() != digest:
        raise EvaluationSignatureError(
            "Expected publication SHA-256 does not match the file being signed"
        )
    key_bytes = _regular_bytes(
        private_key_path, label="Private key", limit=_MAX_KEY_BYTES
    )
    try:
        private_key = serialization.load_pem_private_key(key_bytes, password=None)
    except (TypeError, ValueError) as exc:
        raise EvaluationSignatureError(
            "Private key must be an unencrypted PEM Ed25519 key"
        ) from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise EvaluationSignatureError("Private key algorithm must be Ed25519")
    payload: dict[str, Any] = {
        "schema": SIGNATURE_SCHEMA,
        "version": SIGNATURE_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "namespace": SIGNATURE_NAMESPACE,
        "publication_sha256": digest,
        "publication_schema": publication["schema"],
        "publication_version": publication["version"],
        "release": publication["release"],
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signer": _required_text(signer, "Signer", 200),
        "public_key_sha256": _public_fingerprint(private_key.public_key()),
    }
    signature = private_key.sign(_message(payload))
    payload["signature"] = base64.b64encode(signature).decode("ascii")
    try:
        publish_json_exclusive(
            Path(output_path).expanduser().resolve(),
            payload,
            label="Evaluation signature",
        )
    except PropertyApplyPolicyError as exc:
        raise EvaluationSignatureError(str(exc)) from exc
    return Path(output_path).expanduser().resolve()


def verify_evaluation_signature(
    publication_path: Path | str,
    signature_path: Path | str,
    public_key_path: Path | str,
) -> EvaluationSignatureVerification:
    """Verify exact publication bytes against detached Ed25519 evidence."""

    raw = _regular_bytes(
        publication_path, label="Publication", limit=_MAX_PUBLICATION_BYTES
    )
    publication = _publication(raw)
    signature_bytes = _regular_bytes(
        signature_path, label="Signature", limit=_MAX_PUBLICATION_BYTES
    )
    try:
        evidence = json.loads(signature_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationSignatureError("Signature evidence is invalid JSON") from exc
    if not isinstance(evidence, dict) or set(evidence) != _SIGNATURE_FIELDS:
        raise EvaluationSignatureError("Signature evidence has an unsupported structure")
    if (
        evidence.get("schema") != SIGNATURE_SCHEMA
        or evidence.get("version") != SIGNATURE_VERSION
        or evidence.get("algorithm") != SIGNATURE_ALGORITHM
        or evidence.get("namespace") != SIGNATURE_NAMESPACE
    ):
        raise EvaluationSignatureError("Signature evidence uses an unsupported contract")
    digest = hashlib.sha256(raw).hexdigest()
    if evidence.get("publication_sha256") != digest:
        raise EvaluationSignatureError("Publication bytes do not match signature evidence")
    if (
        evidence.get("publication_schema") != publication["schema"]
        or evidence.get("publication_version") != publication["version"]
        or evidence.get("release") != publication["release"]
    ):
        raise EvaluationSignatureError("Publication identity does not match signature evidence")
    public_bytes = _regular_bytes(
        public_key_path, label="Public key", limit=_MAX_KEY_BYTES
    )
    try:
        public_key = serialization.load_pem_public_key(public_bytes)
    except (TypeError, ValueError) as exc:
        raise EvaluationSignatureError(
            "Public key must be a PEM Ed25519 key"
        ) from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise EvaluationSignatureError("Public key algorithm must be Ed25519")
    fingerprint = _public_fingerprint(public_key)
    if evidence.get("public_key_sha256") != fingerprint:
        raise EvaluationSignatureError("Trusted public key fingerprint does not match")
    try:
        decoded_signature = base64.b64decode(
            evidence.get("signature", ""), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise EvaluationSignatureError("Signature encoding is invalid") from exc
    if len(decoded_signature) != 64:
        raise EvaluationSignatureError("Ed25519 signature length is invalid")
    signed_statement = dict(evidence)
    signed_statement.pop("signature")
    try:
        public_key.verify(decoded_signature, _message(signed_statement))
    except InvalidSignature as exc:
        raise EvaluationSignatureError("Ed25519 signature verification failed") from exc
    signer = _required_text(evidence.get("signer"), "Signer", 200)
    signed_at = _required_text(evidence.get("signed_at"), "Signed time", 100)
    try:
        parsed_signed_at = datetime.fromisoformat(signed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationSignatureError("Signed time is invalid") from exc
    if parsed_signed_at.tzinfo is None:
        raise EvaluationSignatureError("Signed time must include a timezone")
    return EvaluationSignatureVerification(
        digest,
        publication["version"],
        publication["release"],
        signer,
        fingerprint,
        signed_at,
    )
