from __future__ import annotations

import json
import locale
import os
import shlex
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Iterable

from autofix.models import BuildStep


@dataclass(frozen=True)
class StepResult:
    label: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["argv"] = list(self.argv)
        data["succeeded"] = self.succeeded
        return data


@dataclass(frozen=True)
class BuildResult:
    root: Path
    steps: tuple[StepResult, ...]
    started_at: str
    report_path: Path | None = None

    @property
    def succeeded(self) -> bool:
        return bool(self.steps) and all(step.succeeded for step in self.steps)

    @property
    def exit_code(self) -> int:
        if self.succeeded:
            return 0
        return self.steps[-1].exit_code if self.steps else 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "started_at": self.started_at,
            "succeeded": self.succeeded,
            "exit_code": self.exit_code,
            "steps": [step.to_dict() for step in self.steps],
            "report_path": str(self.report_path) if self.report_path else None,
        }


def parse_command(command: str) -> BuildStep:
    # Parse quoting consistently on every platform. In particular, posix=False
    # preserves the quote characters around `python -c "..."`, which changes
    # the meaning of the Python program instead of merely grouping the argument.
    argv = tuple(shlex.split(command, posix=True))
    if not argv:
        raise ValueError("The command cannot be empty")
    return BuildStep(label="Custom command", argv=argv)


def _decode(output: bytes | None) -> str:
    if not output:
        return ""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return output.decode(encoding, errors="replace")


def _execution_argv(project_root: Path, step: BuildStep) -> list[str]:
    """Resolve an explicitly relative project executable for Windows CreateProcess."""

    argv = list(step.argv)
    executable = Path(argv[0])
    if not executable.is_absolute() and executable.parent != Path("."):
        candidate = (project_root / executable).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError:
            return argv
        if candidate.is_file():
            argv[0] = str(candidate)
    return argv


def _save_report(root: Path, result: BuildResult) -> Path:
    report_dir = root / ".autofix" / "runs"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = report_dir / f"{stamp}.json"
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_build(
    root: Path | str,
    steps: Iterable[BuildStep],
    *,
    timeout_seconds: float = 300,
    save_report: bool = True,
) -> BuildResult:
    """Run build steps without a shell, stopping at the first failure."""

    project_root = Path(root).expanduser().resolve()
    started_at = datetime.now(timezone.utc).isoformat()
    results: list[StepResult] = []

    # A unique bytecode cache prevents same-size, same-second Python edits from
    # being verified against stale .pyc files after a repair is applied.
    with tempfile.TemporaryDirectory(prefix="autofix-pycache-") as pycache:
        environment = os.environ.copy()
        environment["PYTHONPYCACHEPREFIX"] = pycache
        go_cache = Path(pycache) / "go-build"
        go_cache.mkdir()
        environment["GOCACHE"] = str(go_cache)
        for step in steps:
            began = monotonic()
            try:
                completed = subprocess.run(
                    _execution_argv(project_root, step),
                    cwd=project_root,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                    env=environment,
                )
                result = StepResult(
                    label=step.label,
                    argv=step.argv,
                    exit_code=completed.returncode,
                    stdout=_decode(completed.stdout),
                    stderr=_decode(completed.stderr),
                    duration_seconds=round(monotonic() - began, 3),
                )
            except FileNotFoundError as exc:
                result = StepResult(
                    label=step.label,
                    argv=step.argv,
                    exit_code=127,
                    stdout="",
                    stderr=str(exc),
                    duration_seconds=round(monotonic() - began, 3),
                )
            except subprocess.TimeoutExpired as exc:
                result = StepResult(
                    label=step.label,
                    argv=step.argv,
                    exit_code=124,
                    stdout=_decode(exc.stdout),
                    stderr=_decode(exc.stderr) or f"Command timed out after {timeout_seconds:g} seconds",
                    duration_seconds=round(monotonic() - began, 3),
                    timed_out=True,
                )
            results.append(result)
            if not result.succeeded:
                break

    base_result = BuildResult(project_root, tuple(results), started_at)
    if not save_report:
        return base_result
    report_path = _save_report(project_root, base_result)
    return BuildResult(project_root, tuple(results), started_at, report_path)

