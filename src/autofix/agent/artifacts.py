"""Create portable, integrity-addressed artifacts from verified repairs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from autofix.agent.patches import ValidatedChange


class RepairArtifactError(ValueError):
    """Raised when a repair artifact cannot be written safely."""


@dataclass(frozen=True)
class PatchArtifact:
    """A standalone unified diff and the digest of its exact bytes."""

    path: Path
    sha256: str


def render_standalone_patch(changes: tuple[ValidatedChange, ...]) -> str:
    """Render deterministic Git-compatible unified diffs in path order."""

    if not changes:
        raise RepairArtifactError("Cannot create a patch without verified changes")
    ordered = sorted(changes, key=lambda change: change.relative_path)
    patch = "".join(change.diff for change in ordered)
    if not patch:
        raise RepairArtifactError("Verified changes contain no textual diff")
    return patch


def save_standalone_patch(
    root: Path | str,
    changes: tuple[ValidatedChange, ...],
    *,
    destination: Path | str | None = None,
) -> PatchArtifact:
    """Write a new patch without overwriting an existing review artifact."""

    project_root = Path(root).expanduser().resolve()
    if destination is None:
        patch_dir = project_root / ".autofix" / "patches"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = patch_dir / f"{stamp}.patch"
    else:
        requested = Path(destination).expanduser()
        path = requested.resolve() if requested.is_absolute() else (Path.cwd() / requested).resolve()
    if path.exists():
        raise RepairArtifactError(f"Patch output already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = render_standalone_patch(changes).encode("utf-8")
        # Exclusive creation prevents a concurrent process from replacing an
        # already reviewed artifact between the existence check and the write.
        with path.open("xb") as stream:
            stream.write(payload)
    except OSError as exc:
        raise RepairArtifactError(f"Unable to write patch artifact: {exc}") from exc
    return PatchArtifact(path, hashlib.sha256(payload).hexdigest())
