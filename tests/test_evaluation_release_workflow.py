from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "evaluation-publication-provenance.yml"


def _workflow() -> tuple[str, dict[str, object]]:
    text = WORKFLOW.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return text, data


def test_release_workflow_has_manual_tag_bound_inputs_and_no_global_permissions() -> None:
    text, workflow = _workflow()
    trigger = workflow["on"]
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"publication_path", "expected_sha256"}
    assert workflow["permissions"] == {}
    assert "This workflow must be dispatched from a Git tag" in text
    assert "publication identity must match its release filename" in text


def test_attest_and_verify_jobs_have_separate_least_privilege_permissions() -> None:
    _, workflow = _workflow()
    jobs = workflow["jobs"]
    attest = jobs["attest"]
    verify = jobs["verify"]
    assert attest["environment"] == "evaluation-publication"
    assert attest["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert verify["permissions"] == {
        "actions": "read",
        "attestations": "read",
        "contents": "read",
    }
    assert "id-token" not in verify["permissions"]
    assert verify["needs"] == "attest"


def test_every_external_action_is_pinned_to_a_full_commit() -> None:
    text, _ = _workflow()
    actions = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
    assert actions
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)


def test_independent_job_enforces_complete_autofix_provenance_policy() -> None:
    text, _ = _workflow()
    required_fragments = (
        "autofix evaluation-verify-provenance verified/publication.json",
        '--repo "$GITHUB_REPOSITORY"',
        '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/evaluation-publication-provenance.yml"',
        '--source-digest "$SOURCE_DIGEST"',
        '--source-ref "$SOURCE_REF"',
        '--expected-publication-sha256 "$PUBLICATION_SHA256"',
        "--output provenance-verification.json",
        "GH_TOKEN: ${{ github.token }}",
    )
    for fragment in required_fragments:
        assert fragment in text


def test_attested_and_downloaded_artifact_paths_are_exact() -> None:
    text, _ = _workflow()
    assert "subject-path: staged/publication.json" in text
    assert "artifact-ids: ${{ needs.attest.outputs.artifact_id }}" in text
    assert "path: verified" in text
    assert "verified/publication.json" in text
