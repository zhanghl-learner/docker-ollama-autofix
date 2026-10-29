"""Stable machine-readable contract for non-interactive CI verification."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

from autofix.agent.diagnostics import environment_failure
from autofix.builders import BuildResult
from autofix.repository import GitWorkspaceStatus


CI_SCHEMA = "autofix-ci-result"
CI_SCHEMA_VERSION = 1


class CIExitCode(IntEnum):
    """Stable process exit codes reserved by ``autofix ci-check``."""

    PASSED = 0
    VERIFICATION_FAILED = 10
    ENVIRONMENT_ERROR = 20
    POLICY_REJECTED = 30
    CONFIGURATION_ERROR = 40
    INTERNAL_ERROR = 70


@dataclass(frozen=True)
class CIResult:
    """One complete CI outcome with a versioned, fixed top-level schema."""

    status: str
    exit_code: CIExitCode
    root: Path
    runner: str | None
    repository: GitWorkspaceStatus | None = None
    build: BuildResult | None = None
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON envelope consumed by CI automation."""

        return {
            "schema": CI_SCHEMA,
            "version": CI_SCHEMA_VERSION,
            "created_at": self.created_at,
            "status": self.status,
            "exit_code": int(self.exit_code),
            "root": str(self.root),
            "runner": self.runner,
            "repository": self.repository.to_dict() if self.repository else None,
            "build": self.build.to_dict() if self.build else None,
            "error": self.error,
        }


def result_from_build(
    root: Path,
    repository: GitWorkspaceStatus,
    build: BuildResult,
    *,
    runner: str,
) -> CIResult:
    """Classify a completed build without exposing tool-specific exit codes."""

    if build.succeeded:
        return CIResult("passed", CIExitCode.PASSED, root, runner, repository, build)
    diagnosis = environment_failure(build)
    if diagnosis:
        return CIResult(
            "environment_error",
            CIExitCode.ENVIRONMENT_ERROR,
            root,
            runner,
            repository,
            build,
            diagnosis,
        )
    return CIResult(
        "verification_failed",
        CIExitCode.VERIFICATION_FAILED,
        root,
        runner,
        repository,
        build,
        "The configured build or test verification failed.",
    )


def save_ci_result(path: Path | str, result: CIResult) -> Path:
    """Atomically replace a requested CI result file with complete JSON."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        os.replace(temporary_path, destination)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return destination
