from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from autofix.agent.artifacts import (
    RepairArtifactError,
    render_standalone_patch,
    save_standalone_patch,
)
from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import validate_changes


def _changes(root: Path, names: tuple[str, ...] = ("b.py", "a.py")):
    for name in names:
        (root / name).write_text(f"value = '{name}:old'\n", encoding="utf-8")
    proposal = RepairProposal(
        "update values",
        tuple(
            FileChange(name, f"value = '{name}:new'\n", "verified change")
            for name in names
        ),
    )
    return validate_changes(root, proposal)


def test_standalone_patch_is_deterministic_and_path_sorted(tmp_path: Path) -> None:
    changes = _changes(tmp_path)

    forward = render_standalone_patch(changes)
    reverse = render_standalone_patch(tuple(reversed(changes)))

    assert forward == reverse
    assert forward.index("--- a/a.py") < forward.index("--- a/b.py")


def test_save_standalone_patch_records_exact_byte_digest(tmp_path: Path) -> None:
    changes = _changes(tmp_path, ("value.py",))
    output = tmp_path / "artifacts" / "repair.patch"

    artifact = save_standalone_patch(tmp_path, changes, destination=output)

    assert artifact.path == output.resolve()
    assert artifact.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.read_bytes().decode("utf-8") == render_standalone_patch(changes)


def test_save_standalone_patch_never_overwrites_existing_file(tmp_path: Path) -> None:
    changes = _changes(tmp_path, ("value.py",))
    output = tmp_path / "repair.patch"
    output.write_text("reviewed artifact\n", encoding="utf-8")

    with pytest.raises(RepairArtifactError, match="already exists"):
        save_standalone_patch(tmp_path, changes, destination=output)

    assert output.read_text(encoding="utf-8") == "reviewed artifact\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_standalone_patch_passes_git_apply_check(tmp_path: Path) -> None:
    changes = _changes(tmp_path, ("value.py",))
    artifact = save_standalone_patch(tmp_path, changes)

    checked = subprocess.run(
        ["git", "-C", str(tmp_path), "apply", "--check", str(artifact.path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert checked.returncode == 0, checked.stderr
