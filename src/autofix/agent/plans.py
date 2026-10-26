"""Persist and load hash-bound, policy-bound deterministic repair plans."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.agent.ollama import FileChange, RepairProposal
from autofix.agent.patches import ValidatedChange, validate_changes
from autofix.builders.docker import DockerConfig, DockerConfigurationError
from autofix.models import BuildStep


class PlanError(ValueError):
    """Raised when a plan is stale, malformed, or violates its bound policy."""

    pass


@dataclass(frozen=True)
class PlannedChange:
    """Verified replacement content bound to the original file hash."""

    path: str
    original_sha256: str
    content: str
    reason: str


@dataclass(frozen=True)
class RepairPlan:
    """A reviewed repair plus the exact policy used to verify it."""

    path: Path
    created_at: str
    model: str
    summary: str
    changes: tuple[PlannedChange, ...]
    version: int = 1
    verification_steps: tuple[BuildStep, ...] | None = None
    verification_runner: str | None = None
    verification_timeout_seconds: float | None = None
    docker_config: DockerConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the plan while preserving v1/v2 compatibility."""

        data: dict[str, Any] = {
            "format": "autofix-repair-plan",
            "version": self.version,
            "created_at": self.created_at,
            "model": self.model,
            "summary": self.summary,
            "changes": [asdict(change) for change in self.changes],
        }
        if self.version >= 2:
            verification: dict[str, Any] = {
                "steps": [step.to_dict() for step in self.verification_steps or ()]
            }
            if self.version >= 3:
                verification["runner"] = self.verification_runner
                verification["timeout_seconds"] = self.verification_timeout_seconds
                if self.docker_config is not None:
                    verification["docker"] = asdict(self.docker_config)
            data["verification"] = verification
        return data

    def to_proposal(self) -> RepairProposal:
        """Adapt reviewed content to the common repair verification pipeline."""

        return RepairProposal(
            self.summary,
            tuple(
                FileChange(change.path, change.content, change.reason)
                for change in self.changes
            ),
        )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def save_repair_plan(
    root: Path | str,
    *,
    model: str,
    summary: str,
    changes: tuple[ValidatedChange, ...],
    verification_steps: tuple[BuildStep, ...] | None = None,
    verification_runner: str = "local",
    verification_timeout_seconds: float = 300,
    docker_config: DockerConfig | None = None,
) -> Path:
    """Write a deterministic plan for changes that already passed verification."""

    project_root = Path(root).expanduser().resolve()
    if not changes:
        raise PlanError("Cannot save an empty repair plan")
    if verification_steps is None and (
        verification_runner != "local" or docker_config is not None
    ):
        raise PlanError("Verification policy requires verification steps")
    if verification_steps is not None and not verification_steps:
        raise PlanError("Cannot save a repair plan with no verification steps")
    if verification_steps is not None and verification_runner not in {"local", "docker"}:
        raise PlanError("Verification runner must be local or docker")
    if (
        verification_steps is not None
        and (
            type(verification_timeout_seconds) not in {int, float}
            or not math.isfinite(verification_timeout_seconds)
            or verification_timeout_seconds <= 0
        )
    ):
        raise PlanError("Verification timeout must be a positive finite number")
    if verification_runner == "docker" and docker_config is None:
        raise PlanError("Docker verification requires a Docker configuration")
    if verification_runner == "local" and docker_config is not None:
        raise PlanError("Local verification cannot include a Docker configuration")
    created_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    plan_dir = project_root / ".autofix" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{stamp}.json"
    planned = tuple(
        PlannedChange(
            change.relative_path,
            _digest(change.path.read_bytes()),
            change.replacement,
            change.reason,
        )
        for change in changes
    )
    version = 3 if verification_steps is not None else 1
    plan = RepairPlan(
        path,
        created_at,
        model,
        summary,
        planned,
        version=version,
        verification_steps=verification_steps,
        verification_runner=verification_runner if verification_steps is not None else None,
        verification_timeout_seconds=(
            float(verification_timeout_seconds) if verification_steps is not None else None
        ),
        docker_config=docker_config,
    )
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_repair_plan(
    path: Path | str,
    root: Path | str,
    *,
    allow_test_changes: bool = False,
    allow_destructive_changes: bool = False,
) -> tuple[RepairPlan, tuple[ValidatedChange, ...]]:
    """Load a plan, revalidate its changes, and reject stale source hashes."""

    plan_path = Path(path).expanduser().resolve()
    project_root = Path(root).expanduser().resolve()
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"Unable to read repair plan: {exc}") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != "autofix-repair-plan"
        or type(version) is not int
        or version not in (1, 2, 3)
    ):
        raise PlanError("Unsupported repair plan")
    created_at, model, summary, raw_changes = (
        data.get("created_at"),
        data.get("model"),
        data.get("summary"),
        data.get("changes"),
    )
    if not all(isinstance(value, str) and value for value in (created_at, model, summary)):
        raise PlanError("Repair plan has invalid metadata")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise PlanError("Repair plan has no changes")
    planned: list[PlannedChange] = []
    for raw in raw_changes:
        if not isinstance(raw, dict) or set(raw) != {"path", "original_sha256", "content", "reason"}:
            raise PlanError("Repair plan has an invalid change record")
        if not all(isinstance(raw[key], str) for key in raw):
            raise PlanError("Repair plan change fields must be strings")
        expected = raw["original_sha256"]
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise PlanError(f"Invalid original hash for {raw['path']}")
        planned.append(PlannedChange(**raw))
    verification_steps: tuple[BuildStep, ...] | None = None
    verification_runner: str | None = None
    verification_timeout_seconds: float | None = None
    docker_config: DockerConfig | None = None
    if version in (2, 3):
        raw_verification = data.get("verification")
        expected_keys = (
            {"steps"}
            if version == 2
            else {"steps", "runner", "timeout_seconds"}
        )
        if not isinstance(raw_verification, dict) or not expected_keys.issubset(
            raw_verification
        ):
            raise PlanError("Repair plan has invalid verification metadata")
        allowed_keys = expected_keys | ({"docker"} if version == 3 else set())
        if set(raw_verification) - allowed_keys:
            raise PlanError("Repair plan has invalid verification metadata")
        raw_steps = raw_verification["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanError("Repair plan has no verification steps")
        parsed_steps: list[BuildStep] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict) or set(raw_step) != {"label", "argv"}:
                raise PlanError("Repair plan has an invalid verification step")
            label, argv = raw_step["label"], raw_step["argv"]
            if not isinstance(label, str) or not label.strip():
                raise PlanError("Repair plan verification labels must be non-empty strings")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(argument, str) and argument for argument in argv)
            ):
                raise PlanError("Repair plan verification argv must contain non-empty strings")
            parsed_steps.append(BuildStep(label, tuple(argv)))
        verification_steps = tuple(parsed_steps)
        if version == 3:
            verification_runner = raw_verification["runner"]
            if not isinstance(verification_runner, str) or verification_runner not in {
                "local",
                "docker",
            }:
                raise PlanError("Repair plan has an invalid verification runner")
            raw_timeout = raw_verification["timeout_seconds"]
            if (
                type(raw_timeout) not in {int, float}
                or not math.isfinite(raw_timeout)
                or raw_timeout <= 0
            ):
                raise PlanError("Repair plan has an invalid verification timeout")
            verification_timeout_seconds = float(raw_timeout)
            raw_docker = raw_verification.get("docker")
            if verification_runner == "local" and "docker" in raw_verification:
                raise PlanError("Local verification plan cannot include Docker settings")
            if verification_runner == "docker":
                if not isinstance(raw_docker, dict) or set(raw_docker) != {
                    "image",
                    "cpus",
                    "memory",
                    "pids_limit",
                    "allow_network",
                }:
                    raise PlanError("Docker repair plan has invalid settings")
                if type(raw_docker["cpus"]) not in {int, float}:
                    raise PlanError("Docker repair plan has an invalid CPU limit")
                if type(raw_docker["pids_limit"]) is not int:
                    raise PlanError("Docker repair plan has an invalid PID limit")
                if type(raw_docker["allow_network"]) is not bool:
                    raise PlanError("Docker repair plan has an invalid network policy")
                if not isinstance(raw_docker["image"], str) or not isinstance(
                    raw_docker["memory"], str
                ):
                    raise PlanError("Docker repair plan has invalid string settings")
                try:
                    docker_config = DockerConfig(**raw_docker)
                except (DockerConfigurationError, TypeError) as exc:
                    raise PlanError(f"Docker repair plan has invalid settings: {exc}") from exc
    plan = RepairPlan(
        plan_path,
        created_at,
        model,
        summary,
        tuple(planned),
        version=version,
        verification_steps=verification_steps,
        verification_runner=verification_runner,
        verification_timeout_seconds=verification_timeout_seconds,
        docker_config=docker_config,
    )
    try:
        changes = validate_changes(
            project_root,
            plan.to_proposal(),
            allow_test_changes=allow_test_changes,
            allow_destructive_changes=allow_destructive_changes,
        )
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    expected_hashes = {change.path: change.original_sha256 for change in plan.changes}
    for change in changes:
        expected = expected_hashes[change.relative_path]
        if _digest(change.path.read_bytes()) != expected:
            raise PlanError(f"Project file changed since preview: {change.relative_path}")
    return plan, changes
