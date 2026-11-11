"""Strict GitHub artifact-attestation verification for evaluation publications."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from autofix.property_apply import PropertyApplyPolicyError, publish_json_exclusive


PROVENANCE_SCHEMA = "autofix-evaluation-provenance-verification"
PROVENANCE_VERSION = 1
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
_MAX_PUBLICATION_BYTES = 4 * 1024 * 1024
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_TRUSTED_ROOT_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GIT_DIGEST = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_WORKFLOW = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$"
)
_SOURCE_REF = re.compile(r"^refs/(?:heads|tags)/[^\s~^:?*\\\[\]]{1,240}$")
_PUBLICATION_FIELDS = {
    "schema", "version", "release", "reviewed_at", "reviewed_by",
    "review_note", "accepted_failed_suite", "benchmark",
    "source_suite_sha256", "source_suite_version", "suite_started_at",
    "suite_passed", "autofix_version", "model", "model_identity", "runner",
    "runtime_identity", "max_attempts", "case_set_sha256", "metrics", "cases",
}


class EvaluationProvenanceError(ValueError):
    """Raised when platform provenance cannot be verified under exact policy."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class EvaluationProvenanceVerification:
    """Privacy-minimized receipt for one successful GitHub verification."""

    artifact_sha256: str
    publication_version: int
    release: str
    repository: str
    signer_workflow: str
    source_digest: str
    source_ref: str
    verification_mode: str
    bundle_sha256: str | None
    trusted_root_sha256: str | None
    attestation_count: int
    gh_version: str
    gh_output_sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return stable policy evidence without paths or raw attestation content."""

        return {
            "schema": PROVENANCE_SCHEMA,
            "version": PROVENANCE_VERSION,
            "verified": True,
            "provider": "github",
            "artifact_type": "evaluation-publication",
            "artifact_sha256": self.artifact_sha256,
            "publication_version": self.publication_version,
            "release": self.release,
            "repository": self.repository,
            "signer_workflow": self.signer_workflow,
            "source_digest": self.source_digest,
            "source_ref": self.source_ref,
            "oidc_issuer": GITHUB_OIDC_ISSUER,
            "predicate_type": SLSA_PROVENANCE_V1,
            "deny_self_hosted_runners": True,
            "verification_mode": self.verification_mode,
            "bundle_sha256": self.bundle_sha256,
            "trusted_root_sha256": self.trusted_root_sha256,
            "attestation_count": self.attestation_count,
            "verifier": {
                "name": "gh",
                "version": self.gh_version,
                "output_sha256": self.gh_output_sha256,
            },
        }


def _regular_bytes(path: Path | str, *, label: str, limit: int) -> bytes:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise EvaluationProvenanceError(f"{label} must be a regular non-symlink file")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise EvaluationProvenanceError(f"{label} must be a regular non-symlink file")
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > limit:
            raise EvaluationProvenanceError(
                f"{label} must contain between 1 and {limit} bytes"
            )
        return resolved.read_bytes()
    except OSError as exc:
        raise EvaluationProvenanceError(f"Unable to read {label.lower()}") from exc


def _publication(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationProvenanceError("Publication must be valid UTF-8 JSON") from exc
    if (
        not isinstance(data, dict)
        or set(data) != _PUBLICATION_FIELDS
        or data.get("schema") != "autofix-evaluation-publication"
        or data.get("version") not in {1, 2}
        or not isinstance(data.get("release"), str)
        or _SAFE_RELEASE.fullmatch(data["release"]) is None
        or not isinstance(data.get("source_suite_sha256"), str)
        or _SHA256.fullmatch(data["source_suite_sha256"]) is None
        or not isinstance(data.get("case_set_sha256"), str)
        or _SHA256.fullmatch(data["case_set_sha256"]) is None
        or data.get("runner") not in {"local", "docker"}
        or not isinstance(data.get("benchmark"), dict)
        or not isinstance(data.get("model_identity"), dict)
        or not isinstance(data.get("runtime_identity"), dict)
        or not isinstance(data.get("metrics"), dict)
        or not isinstance(data.get("cases"), list)
    ):
        raise EvaluationProvenanceError("Publication has an unsupported identity")
    return data


def _validated_policy(
    repository: str, signer_workflow: str, source_digest: str, source_ref: str
) -> tuple[str, str, str, str]:
    repository = repository.strip()
    signer_workflow = signer_workflow.strip()
    source_digest = source_digest.strip().lower()
    source_ref = source_ref.strip()
    if _REPOSITORY.fullmatch(repository) is None:
        raise EvaluationProvenanceError("Repository must use exact owner/repository form")
    if _WORKFLOW.fullmatch(signer_workflow) is None:
        raise EvaluationProvenanceError(
            "Signer workflow must use owner/repository/.github/workflows/name.yml form"
        )
    if _GIT_DIGEST.fullmatch(source_digest) is None:
        raise EvaluationProvenanceError("Source digest must be a 40- or 64-digit Git digest")
    if _SOURCE_REF.fullmatch(source_ref) is None:
        raise EvaluationProvenanceError("Source ref must be an exact refs/heads/... or refs/tags/... value")
    return repository, signer_workflow, source_digest, source_ref


def _run(
    runner: CommandRunner, command: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationProvenanceError("Unable to execute GitHub CLI safely") from exc


def _validate_gh_output(raw: str, artifact_sha256: str) -> int:
    try:
        results = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvaluationProvenanceError("GitHub CLI returned invalid verification JSON") from exc
    if not isinstance(results, list) or not results:
        raise EvaluationProvenanceError("GitHub CLI returned no verified attestations")
    for result in results:
        verification = result.get("verificationResult") if isinstance(result, dict) else None
        statement = verification.get("statement") if isinstance(verification, dict) else None
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(statement, dict) or statement.get("predicateType") != SLSA_PROVENANCE_V1:
            raise EvaluationProvenanceError("Verified attestation has an unexpected predicate type")
        if not isinstance(subjects, list) or not any(
            isinstance(subject, dict)
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == artifact_sha256
            for subject in subjects
        ):
            raise EvaluationProvenanceError("Verified attestation subject does not match publication SHA-256")
    return len(results)


def verify_github_evaluation_provenance(
    publication_path: Path | str,
    *,
    repository: str,
    signer_workflow: str,
    source_digest: str,
    source_ref: str,
    expected_publication_sha256: str,
    bundle_path: Path | str | None = None,
    trusted_root_path: Path | str | None = None,
    output_path: Path | str | None = None,
    command_runner: CommandRunner = subprocess.run,
    gh_executable: str | None = None,
) -> EvaluationProvenanceVerification:
    """Verify GitHub OIDC provenance and optionally publish an immutable receipt."""

    repository, signer_workflow, source_digest, source_ref = _validated_policy(
        repository, signer_workflow, source_digest, source_ref
    )
    raw = _regular_bytes(
        publication_path, label="Publication", limit=_MAX_PUBLICATION_BYTES
    )
    publication = _publication(raw)
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_publication_sha256.lower() != artifact_sha256:
        raise EvaluationProvenanceError(
            "Expected publication SHA-256 does not match the file being verified"
        )
    if (bundle_path is None) != (trusted_root_path is None):
        raise EvaluationProvenanceError(
            "Offline verification requires both bundle and trusted root"
        )
    bundle_sha256 = None
    trusted_root_sha256 = None
    if bundle_path is not None and trusted_root_path is not None:
        bundle_raw = _regular_bytes(bundle_path, label="Bundle", limit=_MAX_BUNDLE_BYTES)
        root_raw = _regular_bytes(
            trusted_root_path, label="Trusted root", limit=_MAX_TRUSTED_ROOT_BYTES
        )
        bundle_sha256 = hashlib.sha256(bundle_raw).hexdigest()
        trusted_root_sha256 = hashlib.sha256(root_raw).hexdigest()
    executable = gh_executable or shutil.which("gh")
    if not executable:
        raise EvaluationProvenanceError("GitHub CLI 'gh' is not available in PATH")
    version_result = _run(command_runner, [executable, "--version"], timeout=10)
    if version_result.returncode != 0 or not version_result.stdout.strip():
        raise EvaluationProvenanceError("Unable to identify the GitHub CLI version")
    gh_version = version_result.stdout.splitlines()[0].strip()[:200]
    command = [
        executable,
        "attestation",
        "verify",
        str(Path(publication_path).expanduser().resolve()),
        "--repo",
        repository,
        "--signer-workflow",
        signer_workflow,
        "--source-digest",
        source_digest,
        "--source-ref",
        source_ref,
        "--cert-oidc-issuer",
        GITHUB_OIDC_ISSUER,
        "--predicate-type",
        SLSA_PROVENANCE_V1,
        "--digest-alg",
        "sha256",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    if bundle_path is not None and trusted_root_path is not None:
        command.extend(
            [
                "--bundle",
                str(Path(bundle_path).expanduser().resolve()),
                "--custom-trusted-root",
                str(Path(trusted_root_path).expanduser().resolve()),
            ]
        )
    verified = _run(command_runner, command, timeout=120)
    if verified.returncode != 0:
        detail = (verified.stderr or verified.stdout).strip().splitlines()
        suffix = f": {detail[-1][:500]}" if detail else ""
        raise EvaluationProvenanceError(f"GitHub attestation verification failed{suffix}")
    count = _validate_gh_output(verified.stdout, artifact_sha256)
    result = EvaluationProvenanceVerification(
        artifact_sha256=artifact_sha256,
        publication_version=publication["version"],
        release=publication["release"],
        repository=repository,
        signer_workflow=signer_workflow,
        source_digest=source_digest,
        source_ref=source_ref,
        verification_mode="offline-bundle" if bundle_path is not None else "online-api",
        bundle_sha256=bundle_sha256,
        trusted_root_sha256=trusted_root_sha256,
        attestation_count=count,
        gh_version=gh_version,
        gh_output_sha256=hashlib.sha256(verified.stdout.encode("utf-8")).hexdigest(),
    )
    if output_path is not None:
        try:
            publish_json_exclusive(
                Path(output_path).expanduser().resolve(),
                result.to_dict(),
                label="Evaluation provenance verification",
            )
        except PropertyApplyPolicyError as exc:
            raise EvaluationProvenanceError(str(exc)) from exc
    return result
